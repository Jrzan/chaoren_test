# -*- coding: utf-8-*-
"""
A Speaker handles audio output from chaoren to the user

Speaker methods:
    say - output 'phrase' as speech
    player - player the audio in 'filename'
    is_available - returns True if the platform supports this implementation
"""
from __future__ import print_function
from __future__ import absolute_import
import os
import platform
import tempfile
import logging
import requests
import datetime
import base64
import hmac
import hashlib
import json
import time
from dateutil import parser as dparser
from abc import ABCMeta, abstractmethod
from uuid import getnode as get_mac
try:
    import urllib.parse as parse
except ImportError:
    import urllib as parse

import argparse

from . import diagnose
from . import chaorenpath
from . import config
from . import player

try:
    import gtts
except ImportError:
    pass

try:
    reload         # Python 2
except NameError:  # Python 3
    from importlib import reload

import sys
reload(sys)
sys.setdefaultencoding('utf8')


class AbstractTTSEngine(object):
    """
    Generic parent class for all speakers
    """
    __metaclass__ = ABCMeta

    @classmethod
    def get_config(cls):
        return {}

    @classmethod
    def get_instance(cls):
        param = cls.get_config()
        print(param)
        instance = cls(**param)
        return instance

    @classmethod
    @abstractmethod
    def is_available(cls):
        return diagnose.check_executable('aplay')

    def __init__(self, **kwargs):
        self._logger = logging.getLogger(__name__)

    @abstractmethod
    def say(self, phrase, *args):
        pass

    def play(self, filename):
        """
        The method has deprecated, use 'mic.Mic.play' instead.
        play wave by aplay
        """
        sound = player.get_sound_manager()
        sound.play_block(filename)


class AbstractMp3TTSEngine(AbstractTTSEngine):
    """
    Generic class that implements the 'play' method for mp3 files
    """
    SLUG = ''

    @classmethod
    def is_available(cls):
        return (super(AbstractMp3TTSEngine, cls).is_available() and
                diagnose.check_python_import('mad'))

    def play_mp3(self, filename, remove=False):
        music = player.get_music_manager()
        music.play_block(filename)

    def removePunctuation(self, phrase):
        to_remove = [
            ',', '/', ':', '\\', '@', '!', '%', '&', '*', '(',
            ')', '{', '}'
        ]
        for note in to_remove:
            phrase = phrase.replace(note, '')
        return phrase

    def say(self, phrase, cache=False):
        self._logger.debug(u"Saying '%s' with '%s'", phrase, self.SLUG)
        h = hashlib.md5()
        h.update(phrase)
        cache_file_path = os.path.join(
            chaorenpath.TEMP_PATH,
            self.SLUG + h.hexdigest() + '.mp3'
        )
        if cache and os.path.exists(cache_file_path):
            self._logger.info(
                "found speech in cache, playing...[%s]" % cache_file_path)
            self.play_mp3(cache_file_path)
        else:
            tmpfile = self.get_speech(phrase)
            if tmpfile is not None:
                self.play_mp3(tmpfile)
                if cache:
                    self._logger.info(
                        "not found speech in cache," +
                        " caching...[%s]" % cache_file_path)
                    os.rename(tmpfile, cache_file_path)
                else:
                    os.remove(tmpfile)

    def get_speech(self, phrase):
        # The subclass needs to implement
        return None


class BaiduTTS(AbstractMp3TTSEngine):
    """
    使用百度语音合成技术
    要使用本模块, 首先到 yuyin.baidu.com 注册一个开发者账号,在应用管理的"查看key"中获得 API Key 和 Secret Key
    填入 profile.yml 中
    """

    SLUG = "baidu-tts"

    def __init__(self, api_key, secret_key, per=0, **args):
        super(self.__class__, self).__init__()
        self._logger = logging.getLogger(__name__)
        self.api_key = api_key
        self.secret_key = secret_key
        self.per = per
        self.token = ''

    @classmethod
    def get_config(cls):
        # Try to get baidu_yuyin config from config
        return config.get('baidu_yuyin', {})

    @classmethod
    def is_available(cls):
        return diagnose.check_network_connection()

    def get_token(self):
        cache = open(os.path.join(chaorenpath.TEMP_PATH, 'baidustt.ini'),
                     'a+')
        try:
            pms = cache.readlines()
            if len(pms) > 0:
                time = pms[0]
                tk = pms[1]
                # 计算token是否过期 官方说明一个月，这里保守29天
                time = dparser.parse(time)
                endtime = datetime.datetime.now()
                if (endtime - time).days <= 29:
                    return tk
        finally:
            cache.close()
        URL = 'http://openapi.baidu.com/oauth/2.0/token'
        params = parse.urlencode({'grant_type': 'client_credentials',
                                  'client_id': self.api_key,
                                  'client_secret': self.secret_key})
        r = requests.get(URL, params=params)
        try:
            r.raise_for_status()
            token = r.json()['access_token']
            return token
        except requests.exceptions.HTTPError:
            self._logger.critical('Token request failed with response: %r',
                                  r.text,
                                  exc_info=True)
            return ''

    def split_sentences(self, text):
        punctuations = ['.', '。', ';', '；', '\n']
        for i in punctuations:
            text = text.replace(i, '@@@')
        return text.split('@@@')

    def get_speech(self, phrase):
        if self.token == '':
            self.token = self.get_token()
        query = {'tex': phrase,
                 'lan': 'zh',
                 'tok': self.token,
                 'ctp': 1,
                 'cuid': str(get_mac())[:32],
                 'per': self.per
                 }
        r = requests.post('http://tsn.baidu.com/text2audio',
                          data=query,
                          headers={'content-type': 'application/json'})
        try:
            r.raise_for_status()
            if r.json()['err_msg'] is not None:
                self._logger.critical('Baidu TTS failed with response: %r',
                                      r.json()['err_msg'],
                                      exc_info=True)
                return None
        except Exception:
            pass
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
            f.write(r.content)
            tmpfile = f.name
            return tmpfile


def get_default_engine_slug():
    return 'osx-tts' if platform.system().lower() == 'darwin' else 'espeak-tts'


def get_engine_by_slug(slug=None):
    """
    Returns:
        A speaker implementation available on the current platform

    Raises:
        ValueError if no speaker implementation is supported on this platform
    """

    if not slug or type(slug) is not str:
        raise TypeError("Invalid slug '%s'", slug)

    selected_engines = filter(lambda engine: hasattr(engine, "SLUG") and
                              engine.SLUG == slug, get_engines())
    if len(selected_engines) == 0:
        raise ValueError("No TTS engine found for slug '%s'" % slug)
    else:
        if len(selected_engines) > 1:
            print("WARNING: Multiple TTS engines found for slug '%s'. " +
                  "This is most certainly a bug." % slug)
        engine = selected_engines[0]
        if not engine.is_available():
            raise ValueError(("TTS engine '%s' is not available (due to " +
                              "missing dependencies, etc.)") % slug)
        return engine


def get_engines():
    def get_subclasses(cls):
        subclasses = set()
        for subclass in cls.__subclasses__():
            subclasses.add(subclass)
            subclasses.update(get_subclasses(subclass))
        return subclasses
    return [tts_engine for tts_engine in
            list(get_subclasses(AbstractTTSEngine))
            if hasattr(tts_engine, 'SLUG') and tts_engine.SLUG]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Chaoren TTS module')
    parser.add_argument('--debug', action='store_true',
                        help='Show debug messages')
    args = parser.parse_args()

    logging.basicConfig()
    if args.debug:
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.DEBUG)

    engines = get_engines()
    available_engines = []
    for engine in get_engines():
        if engine.is_available():
            available_engines.append(engine)
    disabled_engines = list(set(engines).difference(set(available_engines)))
    print("Available TTS engines:")
    for i, engine in enumerate(available_engines, start=1):
        print("%d. %s" % (i, engine.SLUG))

    print("")
    print("Disabled TTS engines:")

    for i, engine in enumerate(disabled_engines, start=1):
        print("%d. %s" % (i, engine.SLUG))

    print("")
    for i, engine in enumerate(available_engines, start=1):
        print("%d. Testing engine '%s'..." % (i, engine.SLUG))
        engine.get_instance().say("This is a test.")
    print("Done.")
