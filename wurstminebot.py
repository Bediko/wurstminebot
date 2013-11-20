#!/usr/bin/env python3
"""Minecraft IRC bot.

Usage:
  wurstminebot [options] [start | stop | restart | status]
  wurstminebot -h | --help
  wurstminebot --version

Options:
  --config=<config>  Path to the config file [default: /opt/wurstmineberg/config/wurstminebot.json].
  -h, --help         Print this message and exit.
  --version          Print version info and exit.
"""

__version__ = '2.1.1'

import sys

sys.path.append('/opt/py')

from TwitterAPI import TwitterAPI
import daemon
import daemon.pidlockfile
from datetime import datetime
import deaths
from docopt import docopt
from ircbotframe import ircBot
import json
import lockfile
import minecraft
import nicksub
import os
import os.path
import random
import re
import requests
import select
import signal
import subprocess
import threading
import time
from datetime import timedelta
import traceback
import xml.sax.saxutils

CONFIG_FILE = '/opt/wurstmineberg/config/wurstminebot.json'
if __name__ == '__main__':
    arguments = docopt(__doc__, version='wurstminebot ' + __version__)
    CONFIG_FILE = arguments['--config']

def _debug_print(msg):
    if config('debug', False):
        print('DEBUG] ' + msg)

def _logtail(timeout=0.5):
    logpath = os.path.join(config('paths')['minecraft_server'], 'logs', 'latest.log')
    with open(logpath) as log:
        lines_read = len(list(log.read().split('\n'))) - 1 # don't yield lines that already existed
    while True:
        time.sleep(timeout)
        with open(logpath) as log:
            lines = log.read().split('\n')
            if len(lines) <= lines_read: # log has restarted
                lines_read = 0
            for line in lines[lines_read:-1]:
                lines_read += 1
                yield line

def config(key=None, default_value=None):
    default_config = {
        'aliases': {},
        'advanced_comment_lines': {
            'death': [],
            'server_join': []
        },
        'comment_lines': {
            'death': ['Well done.'],
            'server_join': []
        },
        'daily_restart': True,
        'debug': False,
        'irc': {
            'channels': [],
            'op_nicks': [],
            'password': '',
            'player_list': 'announce',
            'port': 6667,
            'quit_messages': ['brb'],
            'ssl': False,
            'topic': None
        },
        'paths': {
            'assets': '/var/www/wurstmineberg.de/assets/serverstatus',
            'keepalive': '/var/local/wurstmineberg/wurstminebot_keepalive',
            'logs': '/opt/wurstmineberg/log',
            'minecraft_server': '/opt/wurstmineberg/server',
            'people': '/opt/wurstmineberg/config/people.json',
            'scripts': '/opt/wurstmineberg/bin'
        },
        'twitter': {
            'screen_name': 'wurstmineberg'
        }
    }
    try:
        with open(CONFIG_FILE) as config_file:
            j = json.load(config_file)
    except:
        j = default_config
    if key is None:
        return j
    return j.get(key, default_config.get(key)) if default_value is None else j.get(key, default_value)

def set_config(config_dict):
    with open(CONFIG_FILE, 'w') as config_file:
        json.dump(config_dict, config_file, sort_keys=True, indent=4, separators=(',', ': '))

def update_config(path=[], value):
    config_dict = config()
    full_config_dict = config_dict
    if len(path) > 1:
        for key in path[:-1]:
            if not isinstance(config_dict, dict):
                raise KeyError('Trying to update a non-dict config key')
            if key not in conf:
                config_dict[key] = {}
            config_dict = config_dict[key]
    if len(path) > 0:
        config_dict[path[-1]] = value
    else:
        full_config_dict = value
    set_config(full_config_dict)

ACHIEVEMENTTWEET = True
DEATHTWEET = True
DST = bool(time.localtime().tm_isdst)
LASTDEATH = ''
LOGLOCK = threading.Lock()
PREVIOUS_TOPIC = None

bot = ircBot(config('irc')['server'], config('irc')['port'], config('irc')['nick'], config('irc')['nick'], password=config('irc')['password'], ssl=config('irc')['ssl'])
bot.log_own_messages = False

twitter = TwitterAPI(config('twitter')['consumer_key'], config('twitter')['consumer_secret'], config('twitter')['access_token_key'], config('twitter')['access_token_secret'])

def _timed_input(timeout=1): #FROM http://stackoverflow.com/a/2904057
    i, o, e = select.select([sys.stdin], [], [], timeout)
    if i:
        return sys.stdin.readline().strip()

class errors:
    botop = 'you must be a bot op to do this'
    log = "I can't find that in my chatlog"
    
    @staticmethod
    def argc(expected, given, atleast=False):
        return ('not enough' if given < expected else 'too many') + ' arguments, expected ' + ('at least ' if atleast else '') + str(expected)
    
    @staticmethod
    def unknown(command=None):
        if command is None or command == '':
            return 'Unknown command. Execute “help commands” for a list of commands, or “help aliases” for a list of aliases.'
        else:
            return '“' + str(command) + '” is not a command. Execute “help commands” for a list of commands, or “help aliases” for a list of aliases.'

def update_all(*args, **kwargs):
    minecraft.update_status()
    minecraft.update_whitelist()
    update_topic(force='reply' in kwargs) # force-update the topic if called from fixstatus command
    threading.Timer(20, minecraft.update_status).start()

class InputLoop(threading.Thread):
    def run(self):
        global LASTDEATH
        try:
            for logLine in _logtail():
                # server log output processing
                _debug_print('[logpipe] ' + logLine)
                match = re.match(minecraft.regexes.timestamp + ' \\[Server thread/INFO\\]: \\* (' + minecraft.regexes.player + ') (.*)', logLine)
                if match:
                    # action
                    player, message = match.group(1, 2)
                    chan = config('irc')['main_channel']
                    sender = nicksub.sub(player, 'minecraft', 'irc')
                    subbed_message = nicksub.textsub(message, 'minecraft', 'irc')
                    bot.log(chan, 'ACTION', sender, [chan], subbed_message)
                    bot.say(chan, '* ' + sender + ' ' + subbed_message)
                else:
                    match = re.match(minecraft.regexes.timestamp + ' \\[Server thread/INFO\\]: <(' + minecraft.regexes.player + ')> (.*)', logLine)
                    if match:
                        player, message = match.group(1, 2)
                        if message.startswith('!') and len(message) > 1:
                            # command
                            cmd = message[1:].split(' ')
                            command(sender=player, chan=None, cmd=cmd[0], args=cmd[1:], context='minecraft')
                        else:
                            # chat message
                            chan = config('irc')['main_channel']
                            sender = nicksub.sub(player, 'minecraft', 'irc')
                            subbed_message = nicksub.textsub(message, 'minecraft', 'irc')
                            bot.log(chan, 'PRIVMSG', sender, [chan], subbed_message)
                            bot.say(chan, '<' + sender + '> ' + subbed_message)
                    else:
                        match = re.match('(' + minecraft.regexes.timestamp + ') \\[Server thread/INFO\\]: (' + minecraft.regexes.player + ') (left|joined) the game', logLine)
                        if match:
                            # join/leave
                            timestamp, player = match.group(1, 2)
                            joined = bool(match.group(3) == 'joined')
                            with open(os.path.join(config('paths')['logs'], 'logins.log')) as loginslog:
                                for line in loginslog:
                                    if player in line:
                                        new_player = False
                                        break
                                else:
                                    new_player = True
                            with open(os.path.join(config('paths')['logs'], 'logins.log'), 'a') as loginslog:
                                print(timestamp + ' ' + player + ' ' + ('joined' if joined else 'left') + ' the game', file=loginslog)
                            if joined:
                                if new_player:
                                    welcome_message = 'Welcome to the server!'
                                else:
                                    welcome_messages = dict(((1, index), 1.0) for index in range(len(config('comment_lines').get('server_join', []))))
                                    with open(config('paths')['people']) as people_json:
                                        people = json.load(people_json)
                                    for person in people:
                                        if person['minecraft'] == player:
                                            if 'description' not in person:
                                                welcome_messages[0, 1] = 1.0
                                            break
                                    else:
                                        welcome_messages[0, 2] = 16.0
                                    for index, adv_welcome_msg in enumerate(config('advanced_comment_lines').get('server_join', [])):
                                        if 'text' not in adv_welcome_msg:
                                            continue
                                        welcome_messages[2, index] = adv_welcome_msg.get('weight', 1.0) * adv_welcome_msg.get('player_weights', {}).get(player, adv_welcome_msg.get('player_weights', {}).get('@default', 1.0))
                                    random_index = random.uniform(0.0, sum(welcome_messages.values()))
                                    index = 0.0
                                    for welcome_message, weight in welcome_messages.items():
                                        if random_index - index < weight:
                                            break
                                        else:
                                            index += weight
                                    else:
                                        welcome_message = (0, 0)
                                if welcome_message == (0, 0):
                                    minecraft.tellraw({'text': 'Hello ' + player + '. Um... sup?', 'color': 'gray'}, player)
                                if welcome_message == (0, 1):
                                    minecraft.tellraw([
                                        {
                                            'text': 'Hello ' + player + ". You still don't have a description for ",
                                            'color': 'gray'
                                        },
                                        {
                                            'text': 'the people page',
                                            'hoverEvent': {
                                                'action': 'show_text',
                                                'value': 'http://wurstmineberg.de/people'
                                            },
                                            'clickEvent': {
                                                'action': 'open_url',
                                                'value': 'http://wurstmineberg.de/people'
                                            },
                                            'color': 'gray'
                                        },
                                        {
                                            'text': '. ',
                                            'color': 'gray'
                                        },
                                        {
                                            'text': 'Write one today',
                                            'clickEvent': {
                                                'action': 'suggest_command',
                                                'value': '!people ' + person + ' description '
                                            },
                                            'color': 'gray'
                                        },
                                        {
                                            'text': '!',
                                            'color': 'gray'
                                        }
                                    ], player)
                                elif welcome_message[0] == 1:
                                    minecraft.tellraw({'text': 'Hello ' + player + '. ' + config('comment_lines')['server_join'][welcome_message[1]], 'color': 'gray'}, player)
                                elif welcome_message[0] == 2:
                                    message_dict = config('advanced_comment_lines')['server_join'][welcome_message[1]]
                                    message_list = message_dict['text']
                                    if isinstance(message_list, str):
                                        message_list = [{'text': message_list, 'color': 'gray'}]
                                    elif isinstance(message_list, dict):
                                        message_list = [message_list]
                                    minecraft.tellraw(([
                                        {
                                            'text': 'Hello ' + player + '. ',
                                            'color': 'gray'
                                        }
                                    ] if message_dict.get('hello_prefix', True) else []) + message_list, player)
                                else:
                                    minecraft.tellraw({'text': 'Hello ' + player + '. How did you do that?', 'color': 'gray'}, player)
                            if config('irc').get('player_list', 'announce') == 'announce':
                                bot.say(config('irc')['main_channel'], nicksub.sub(player, 'minecraft', 'irc') + ' ' + ('joined' if joined else 'left') + ' the game')
                            update_all()
                        else:
                            match = re.match(minecraft.regexes.timestamp + ' \\[Server thread/INFO\\]: (' + minecraft.regexes.player + ') has just earned the achievement \\[(.+)\\]$', logLine)
                            if match:
                                # achievement
                                player, achievement = match.group(1, 2)
                                if ACHIEVEMENTTWEET:
                                    tweet = '[Achievement Get] ' + nicksub.sub(player, 'minecraft', 'twitter') + ' got ' + achievement
                                    if len(tweet) <= 140:
                                        tweet_request = twitter.request('statuses/update', {'status': tweet})
                                        if 'id' in tweet_request.json():
                                            twid = 'https://twitter.com/wurstmineberg/status/' + str(tweet_request.json()['id'])
                                        else:
                                            twid = 'error ' + str(tweet_request.status_code)
                                    else:
                                        twid = 'too long for twitter'
                                else:
                                    twid = 'achievement tweets are disabled'
                                bot.say(config('irc')['main_channel'], 'Achievement Get: ' + nicksub.sub(player, 'minecraft', 'irc') + ' got ' + achievement + ' [' + twid + ']')
                            else:
                                for deathid, death in enumerate(deaths.regexes):
                                    match = re.match('(' + minecraft.regexes.timestamp + ') \\[Server thread/INFO\\]: (' + minecraft.regexes.player + ') ' + death + '$', logLine)
                                    if not match:
                                        continue
                                    # death
                                    timestamp, player = match.group(1, 2)
                                    groups = match.groups()[2:]
                                    message = deaths.partial_message(deathid, groups)
                                    with open(os.path.join(config('paths')['logs'], 'deaths.log'), 'a') as deathslog:
                                        print(timestamp + ' ' + player + ' ' + message, file=deathslog)
                                    if DEATHTWEET:
                                        if player + ' ' + message == LASTDEATH:
                                            comment = ' … Again.' # This prevents botspam if the same player dies lots of times (more than twice) for the same reason.
                                        else:
                                            death_comments = config('comment_lines').get('death', ['Well done.'])
                                            if deathid == 7: # was blown up by Creeper
                                                death_comments.append('Creepers gonna creep.')
                                            if deathid == 28: # was slain by Zombie
                                                death_comments.append('Zombies gonna zomb.')
                                            comment = ' … ' + random.choice(death_comments)
                                        LASTDEATH = player + ' ' + message
                                        tweet = '[DEATH] ' + nicksub.sub(player, 'minecraft', 'twitter') + ' ' + nicksub.textsub(message, 'minecraft', 'twitter', strict=True)
                                        if len(tweet + comment) <= 140:
                                            tweet += comment
                                        if len(tweet) <= 140:
                                            tweet_request = twitter.request('statuses/update', {'status': tweet})
                                            if 'id' in tweet_request.json():
                                                twid = 'https://twitter.com/wurstmineberg/status/' + str(tweet_request.json()['id'])
                                                minecraft.tellraw({'text': 'Your fail has been reported. Congratulations.', 'color': 'gold', 'clickEvent': {'action': 'open_url', 'value': twid}})
                                            else:
                                                twid = 'error ' + str(tweet_request.status_code)
                                                minecraft.tellraw({'text': 'Your fail has ', 'color': 'gold', 'extra': [{'text': 'not', 'color': 'red'}, {'text': ' been reported because of '}, {'text': 'reasons', 'hoverEvent': {'action': 'show_text', 'value': str(tweet_request.status_code)}}, {'text': '.'}]})
                                        else:
                                            twid = 'too long for twitter'
                                            minecraft.tellraw({'text': 'Your fail has ', 'color': 'gold', 'extra': [{'text': 'not', 'color': 'red'}, {'text': ' been reported because it was too long.'}]})
                                    else:
                                        twid = 'deathtweets are disabled'
                                    bot.say(config('irc')['main_channel'], nicksub.sub(player, 'minecraft', 'irc') + ' ' + nicksub.textsub(message, 'minecraft', 'irc', strict=True) + ' [' + twid + ']')
                                    break
                if not bot.keepGoing:
                    break
        except SystemExit:
            _debug_print('Exit in log input loop')
            TimeLoop.stop()
            raise
        except:
            _debug_print('Exception in log input loop:')
            if config('debug', False):
                traceback.print_exc()
            self.run()

class TimeLoop(threading.Thread):
    def __init__(self):
        super().__init__()
        self.stopped = False
    
    def run(self):
        #FROM http://stackoverflow.com/questions/9918972/running-a-line-in-1-hour-intervals-in-python
        # modified to work with leap seconds
        while True:
            # sleep for the remaining seconds until the next hour
            time.sleep(3601 - time.time() % 3600)
            if self.stopped:
                break
            telltime(comment=True, restart=config('daily_restart', True))
    
    def stop(self):
        self.stopped = True

TimeLoop = TimeLoop()

def telltime(func=None, comment=False, restart=False):
    if func is None:
        def func(msg):
            for line in msg.splitlines():
                minecraft.tellraw({'text': line, 'color': 'gold'})
        
        custom_func = False
    else:
        custom_func = True
    def warning(msg):
        if custom_func:
            func(msg)
        else:
            for line in msg.splitlines():
                minecraft.tellraw({'text': line, 'color': 'red'})
    
    global DST
    global PREVIOUS_TOPIC
    localnow = datetime.now()
    utcnow = datetime.utcnow()
    dst = bool(time.localtime().tm_isdst)
    if dst != DST:
        if dst:
            func('Daylight saving time is now in effect.')
        else:
            func('Daylight saving time is no longer in effect.')
    func('The time is ' + localnow.strftime('%H:%M') + ' (' + utcnow.strftime('%H:%M') + ' UTC)')
    if comment:
        if dst != DST:
            pass
        elif localnow.hour == 0:
            func('Dark outside, better play some Minecraft.')
        elif localnow.hour == 1:
            func("You better don't stay up all night again.")
        elif localnow.hour == 2:
            func('Some late night mining always cheers me up.')
            time.sleep(10)
            func('...Or redstoning. Or building. Whatever floats your boat.')
        elif localnow.hour == 3:
            func('Seems like you are having fun.')
            time.sleep(60)
            func("I heard that zombie over there talk trash about you. Thought you'd wanna know...")
        elif localnow.hour == 4:
            func('Getting pretty late, huh?')
        elif localnow.hour == 5:
            warning('It is really getting late. You should go to sleep.')
        elif localnow.hour == 6:
            func('Are you still going, just starting or asking yourself the same thing?')
        elif localnow.hour == 11 and localnow.minute < 5 and restart:
            players = minecraft.online_players()
            if len(players):
                warning('The server is going to restart in 5 minutes.')
                time.sleep(240)
                warning('The server is going to restart in 60 seconds.')
                time.sleep(50)
            PREVIOUS_TOPIC = (config('irc')['topic'] + ' | ' if 'topic' in config('irc') and config('irc')['topic'] is not None else '') + 'The server is restarting…'
            bot.topic(config('irc')['main_channel'], PREVIOUS_TOPIC)
            minecraft.stop(reply=func)
            time.sleep(30)
            if minecraft.start(reply=func):
                if len(players):
                    bot.say(', '.join(players) + ': The server has restarted.')
            else:
                bot.say('Please help! Something went wrong with the server restart!')
            update_topic()
    DST = dst

def update_topic(force=False):
    global PREVIOUS_TOPIC
    players = minecraft.online_players() if config('irc').get('player_list', 'announce') == 'topic' else []
    player_list = ('Currently online: ' + ', '.join(players)) if len(players) else ''
    topic = config('irc').get('topic')
    if topic is None:
        new_topic = player_list
    elif len(players):
        new_topic = topic + ' | ' + player_list
    else:
        new_topic = topic
    if force or PREVIOUS_TOPIC != new_topic:
        bot.topic(config('irc')['main_channel'], new_topic)
    PREVIOUS_TOPIC = new_topic

def mwiki_lookup(article=None, args=[], botop=False, reply=None, sender=None):
    if reply is None:
        def reply(*args, **kwargs):
            pass
    
    if article is None:
        if args is None:
            article = ''
        if isinstance(args, str):
            article = args
        elif isinstance(args, list):
            article = '_'.join(args)
        else:
            reply('Unknown article')
            return 'Unknown article'
    match = re.match('http://(?:minecraft\\.gamepedia\\.com|minecraftwiki\\.net(?:/wiki)?)/(.*)', article)
    if match:
        article = match.group(1)
    request = requests.get('http://minecraft.gamepedia.com/' + article, params={'action': 'raw'})
    if request.status_code == 200:
        if request.text.lower().startswith('#redirect'):
            match = re.match('#[Rr][Ee][Dd][Ii][Rr][Ee][Cc][Tt] \\[\\[(.+)(\\|.*)?\\]\\]', request.text)
            if match:
                redirect_target = 'http://minecraft.gamepedia.com/' + re.sub(' ', '_', match.group(1))
                reply('Redirect ' + redirect_target)
                return 'Redirect ' + redirect_target
            else:
                reply('Broken redirect')
                return 'Broken redirect'
        else:
            reply('Article http://minecraft.gamepedia.com/' + article)
            return 'Article http://minecraft.gamepedia.com/' + article
    else:
        reply('Error ' + str(request.status_code))
        return 'Error ' + str(request.status_code)

def command(sender, chan, cmd, args, context='irc', reply=None, reply_format=None):
    if reply is None:
        if reply_format == 'tellraw' or context == 'minecraft':
            reply_format = 'tellraw'
            def reply(msg):
                if isinstance(msg, str):
                    for line in msg.splitlines():
                        minecraft.tellraw({'text': line, 'color': 'gold'}, '@a' if sender is None else sender)
                else:
                    minecraft.tellraw(msg, '@a' if sender is None else sender)
        else:
            def reply(msg):
                if context == 'irc':
                    if not sender:
                        for line in msg.splitlines():
                            bot.say(config('irc')['main_channel'] if chan is None else chan, line)
                    elif chan:
                        for line in msg.splitlines():
                            bot.say(chan, sender + ': ' + line)
                    else:
                        for line in msg.splitlines():
                            bot.say(sender, line)
                elif context == 'console':
                    print(msg)
    
    def warning(msg):
        if reply_format == 'tellraw':
            reply({'text': msg, 'color': 'red'})
        else:
            reply(msg)
    
    def _command_achievementtweet(args=[], botop=False, reply=reply, sender=sender):
        global ACHIEVEMENTTWEET
        if not len(args):
            reply('Achievement tweeting is currently ' + ('enabled' if ACHIEVEMENTTWEET else 'disabled'))
        elif args[0] == 'on':
            ACHIEVEMENTTWEET = True
            reply('Achievement tweeting is now enabled')
        elif args[0] == 'off':
            def _reenable_achievement_tweets():
                global ACHIEVEMENTTWEET
                ACHIEVEMENTTWEET = True
            
            if len(args) >= 2:
                match = re.match('([0-9]+)([dhms])', args[1])
                if match:
                    number, unit = match.group(1, 2)
                    number *= {'d': 86400, 'h': 3600, 'm': 60, 's': 1}[unit]
                elif re.match('[0-9]+', args[1]):
                    number = int(args[1])
                else:
                    warning(args[1] + ' is not a time value')
                    return
                threading.Timer(number, _reenable_death_tweets).start()
            elif not botop:
                warning(errors.botop)
                return
            ACHIEVEMENTTWEET = False
            reply('Achievement tweeting is now disabled')
        else:
            warning('Usage: achievementtweet [on | off [<time>]]')
    
    def _command_alias(args=[], botop=False, reply=reply, sender=sender):
        aliases = config('aliases')
        if len(args) == 0:
            warning('Usage: alias <alias_name> [<text>...]')
        elif len(args) == 1:
            if botop:
                if str(args[0]) in aliases:
                    deleted_alias = str(aliases[str(args[0])])
                    del aliases[str(args[0])]
                    config_update(['aliases'], aliases)
                    reply('Alias deleted. (Was “' + deleted_alias + '”)')
                else:
                    warning('The alias you' + (' just ' if random.randrange(0, 1) else ' ') + 'tried to delete ' + ("didn't" if random.randrange(0, 1) else 'did not') + (' even ' if random.randrange(0, 1) else ' ') + 'exist' + (' in the first place!' if random.randrange(0, 1) else '!') + (" So I guess everything's fine then?" if random.randrange(0, 1) else '')) # fun with randomized replies
            else:
                warning(errors.botop)
        elif str(args[0]) in aliases and not botop:
            warning(errors.botop)
        else:
            alias_existed = str(args[0]) in aliases
            aliases[str(args[0])] = ' '.join(aliases[1:])
            config_update(['aliases'], aliases)
            reply('Alias ' + ('edited' if alias_existed else 'added') + ', but hidden because there is a command with the same name.' if str(srgs[0]) in commands else 'Alias added.')
    
    def _command_command(args=[], botop=False, reply=reply, sender=sender):
        if args[0]:
            reply(minecraft.command(args[0], args[1:]))
        else:
            warning(errors.argc(1, len(args), atleast=True))
    
    def _command_deathtweet(args=[], botop=False, reply=reply, sender=sender):
        global DEATHTWEET
        if not len(args):
            reply('Deathtweeting is currently ' + ('enabled' if DEATHTWEET else 'disabled'))
        elif args[0] == 'on':
            DEATHTWEET = True
            reply('Deathtweeting is now enabled')
        elif args[0] == 'off':
            def _reenable_death_tweets():
                global DEATHTWEET
                DEATHTWEET = True
            
            if len(args) >= 2:
                match = re.match('([0-9]+)([dhms])', args[1])
                if match:
                    number, unit = match.group(1, 2)
                    number *= {'d': 86400, 'h': 3600, 'm': 60, 's': 1}[unit]
                elif re.match('[0-9]+', args[1]):
                    number = int(args[1])
                else:
                    warning(args[1] + ' is not a time value')
                    return
                threading.Timer(number, _reenable_death_tweets).start()
            elif not botop:
                warning(errors.botop)
                return
            DEATHTWEET = False
            reply('Deathtweeting is now disabled')
        else:
            warning('Usage: deathtweet [on | off [<time>]]')
    
    def _command_lastseen(args=[], botop=False, reply=reply, sender=sender):
        global LAST
        if len(args):
            player = args[0]
            try:
                person = nicksub.Person(player, context=context)
            except (ValueError, nicksub.PersonNotFoundError):
                try:
                    person = nicksub.Person(player, context='minecraft')
                except (ValueError, nicksub.PersonNotFoundError):
                    try:
                        person = nicksub.Person(player)
                    except nicksub.PersonNotFoundError:
                        warning('No such person')
                        return
            if person.minecraft is None:
                warning('No Minecraft nick for this person')
                return
            if person.minecraft in minecraft.online_players():
                if reply_format == 'tellraw':
                    reply([
                        {
                            'text': player,
                            'hoverEvent': {
                                'action': 'show_text',
                                'value': mcplayer + ' in Minecraft'
                            },
                            'clickEvent': {
                                'action': 'suggest_command',
                                'value': mcplayer + ': '
                            },
                            'color': 'gold',
                        },
                        {
                            'text': ' is currently on the server.',
                            'color': 'gold'
                        }
                    ])
                else:
                    reply(player + ' is currently on the server.')
            else:
                with LOGLOCK:
                    lastseen = minecraft.last_seen(mcplayer)
                    if lastseen is None:
                        reply('I have not seen ' + player + ' on the server yet.')
                    else:
                        if lastseen.date() == datetime.utcnow().date():
                            datestr = 'today at ' + lastseen.strftime('%H:%M UTC')
                            tellraw_date = [
                                {
                                    'text': 'today',
                                    'hoverEvent': {
                                        'action': 'show_text',
                                        'value': lastseen.strftime('%Y-%m-%d')
                                    },
                                    'color': 'gold'
                                },
                                {
                                    'text': ' at ' + lastseen.strftime('%H:%M UTC.'),
                                    'color': 'gold'
                                }
                            ]
                        elif lastseen.date() == datetime.utcnow().date() - timedelta(days=1):
                            datestr = 'yesterday at ' + lastseen.strftime('%H:%M UTC')
                            tellraw_date = [
                                {
                                    'text': 'yesterday',
                                    'hoverEvent': {
                                        'action': 'show_text',
                                        'value': lastseen.strftime('%Y-%m-%d')
                                    },
                                    'color': 'gold'
                                },
                                {
                                    'text': ' at ' + lastseen.strftime('%H:%M UTC.'),
                                    'color': 'gold'
                                }
                            ]
                        else:
                            datestr = lastseen.strftime('on %Y-%m-%d at %H:%M UTC')
                            tellraw_date = [
                                {
                                    'text': datestr + '.',
                                    'color': 'gold'
                                }
                            ]
                        if reply_format == 'tellraw':
                            reply([
                                {
                                    'text': player,
                                    'hoverEvent': {
                                        'action': 'show_text',
                                        'value': mcplayer + ' in Minecraft'
                                    },
                                    'color': 'gold',
                                },
                                {
                                    'text': ' was last seen ',
                                    'color': 'gold'
                                }
                            ] + tellraw_date)
                        else:
                            reply(player + ' was last seen ' + datestr + '.')
        else:
            warning(errors.argc(1, len(args)))
    
    def _command_leak(args=[], botop=False, reply=reply, sender=sender):
        messages = [(msg_type, msg_sender, msg_text) for msg_type, msg_sender, msg_headers, msg_text in bot.channel_data[config('irc')['main_channel']]['log'] if msg_type == 'ACTION' or (msg_type == 'PRIVMSG' and (not msg_text.startswith('!')) and (not msg_text.startswith(config('irc')['nick'] + ': ')) and (not msg_text.startswith(config('irc')['nick'] + ', ')))]
        if len(args) == 0:
            if len(messages):
                messages = [messages[-1]]
            else:
                warning(errors.log)
                return
        elif len(args) == 1:
            if re.match('[0-9]+$', args[0]) and len(messages) >= int(args[0]):
                messages = messages[-int(args[0]):]
            else:
                warning(errors.log)
                return
        else:
            warning(errors.argc(1, len(args)))
            return
        tweet = '\n'.join(((('* ' + nicksub.sub(msg_sender, 'irc', 'twitter') + ' ') if msg_type == 'ACTION' else ('<' + nicksub.sub(msg_sender, 'irc', 'twitter') + '> ')) + nicksub.textsub(message, 'irc', 'twitter')) for msg_type, msg_sender, message in messages)
        if len(tweet + ' #ircleaks') <= 140:
            if '\n' in tweet:
                tweet += '\n#ircleaks'
            else:
                tweet += ' #ircleaks'
        command(None, chan, 'tweet', [tweet], context='twitter', reply=reply, reply_format=reply_format)
    
    def _command_opt(args=[], botop=False, reply=reply, sender=sender):
        if len(args) == 0:
            warning(errors.argc(1, len(args), atleast=True))
            return
        option = str(args[0])
        with open(config('paths')['people']) as people_json:
            people = json.load(people_json)
        for person in people:
            if context == 'irc':
                if sender in person.get('irc', {}).get('nicks', []):
                    break
            elif person.get('id' if context is None else context) == sender:
                break
        else:
            warning("couldn't find you in people.json")
            return None
        if len(args) == 1:
            default_true_options = [] # These options are on by default. All other options are off by default.
            if 'options' in person and str(args[0]) in person['options']:
                flag = bool(person['options'][str(args[0])])
                is_default = False
            else:
                flag = bool(args[0] in default_true_options)
                is_default = True
            reply('option ' + str(args[0]) + ' is ' + ('on' if flag else 'off') + ' ' + ('by default' if is_default else 'for you'))
            return flag
        else:
            flag = bool(args[1] in [True, 1, '1', 'true', 'True', 'on', 'yes', 'y', 'Y'])
            if 'options' not in person:
                person['options'] = {}
            person['options'][str(args[0])] = flag
            with open(config('paths')['people'], 'w') as people_json:
                json.dump(people, people_json, indent=4, separators=(',', ': '), sort_keys=True)
            reply('option ' + str(args[0]) + ' is now ' + ('on' if flag else 'off') + ' for you')
            return flag
    
    def _command_pastemojira(args=[], botop=False, reply=reply, sender=sender):
        link = True
        if len(args) == 3 and args[2] == 'nolink':
            link = False
            args = args[:2]
        elif len(args) == 2 and args[1] == 'nolink':
            link = False
            args = [args[0]]
        if len(args) == 2:
            project_key = str(args[0])
            try:
                issue_id = int(args[1])
            except ValueError:
                warning('Invalid issue ID: ' + str(args[0]))
                return
        elif len(args) == 1:
            match = re.match('(https?://mojang.atlassian.net/browse/)?([A-Z]+)-([0-9]+)', str(args[0]))
            if match:
                project_key = str(match.group(2))
                issue_id = int(match.group(3))
            else:
                project_key = 'MC'
                try:
                    issue_id = int(args[0])
                except ValueError:
                    warning('Invalid issue ID: ' + str(args[0]))
        else:
            reply('http://mojang.atlassian.net/browse/MC')
            return
        request = requests.get('http://mojang.atlassian.net/browse/' + project_key + '-' + str(issue_id))
        if request.status_code == 200:
            match = re.match('<title>\\[([A-Z]+)-([0-9]+)\\] (.+) - Mojira</title>', request.text.splitlines()[18])
            if not match:
                warning('could not get title')
                return
            project_key, issue_id, title = match.group(1, 2, 3)
            if reply_format == 'tellraw':
                reply({
                    'text': '[' + project_key + '-' + issue_id + '] ' + title,
                    'color': 'gold',
                    'clickEvent': {
                        'action': 'open_url',
                        'value': 'http://mojang.atlassian.net/browse/' + project_key + '-' + issue_id
                    }
                })
            else:
                reply('[' + project_key + '-' + issue_id + '] ' + title + (' [http://mojang.atlassian.net/browse/' + project_key + '-' + issue_id + ']' if link else ''))
        else:
            warning('Error ' + str(request.status_code))
            return
    
    def _command_pastetweet(args=[], botop=False, reply=reply, sender=sender):
        link = True
        if len(args) == 2 and args[1] == 'nolink':
            link = False
            args = [args[0]]
        if len(args) == 1:
            match = re.match('https?://twitter\\.com/[0-9A-Z_a-z]+/status/([0-9]+)', str(args[0]))
            twid = match.group(1) if match else args[0]
            request = twitter.request('statuses/show', {'id': twid})
            if 'id' in request.json():
                if 'retweeted_status' in request.json():
                    retweeted_request = twitter.request('statuses/show', {'id': request.json()['retweeted_status']['id']})
                    tweet_author = '<@' + request.json()['user']['screen_name'] + ' RT @' + retweeted_request.json()['user']['screen_name'] + '> '
                    tweet_author_tellraw = [
                        {
                            'text': '@' + request.json()['user']['screen_name'],
                            'clickEvent': {
                                'action': 'open_url',
                                'value': 'https://twitter.com/' + request.json()['user']['screen_name']
                            },
                            'color': 'gold'
                        },
                        {
                            'text': ' RT ',
                            'color': 'gold'
                        },
                        {
                            'text': '@' + retweeted_request.json()['user']['screen_name'],
                            'clickEvent': {
                                'action': 'open_url',
                                'value': 'https://twitter.com/' + retweeted_request.json()['user']['screen_name']
                            },
                            'color': 'gold'
                        }
                    ]
                    text = xml.sax.saxutils.unescape(retweeted_request.json()['text'])
                else:
                    tweet_author = '<@' + request.json()['user']['screen_name'] + '> '
                    tweet_author_tellraw = [
                        {
                            'text': '@' + request.json()['user']['screen_name'],
                            'clickEvent': {
                                'action': 'open_url',
                                'value': 'https://twitter.com/' + request.json()['user']['screen_name']
                            },
                            'color': 'gold'
                        }
                    ]
                    text = xml.sax.saxutils.unescape(request.json()['text'])
                tweet_url = 'https://twitter.com/' + request.json()['user']['screen_name'] + '/status/' + request.json()['id_str']
                if reply_format == 'tellraw':
                    reply({
                        'text': '<',
                        'color': 'gold',
                        'extra': tweet_author_tellraw + [
                            {
                                'text': '> ' + text,
                                'color': 'gold'
                            }
                        ] + ([
                            {
                                'text': ' [',
                                'color': 'gold'
                            },
                            {
                                'text': tweet_url,
                                'clickEvent': {
                                    'action': 'open_url',
                                    'value': tweet_url
                                },
                                'color': 'gold'
                            },
                            {
                                'text': ']',
                                'color': 'gold'
                            }
                        ] if link else [])
                    })
                else:
                    reply(tweet_author + text + ((' [' + tweet_url + ']') if link else ''))
            else:
                warning('Error ' + str(request.status_code))
        else:
            warning(errors.argc(1, len(args)))
    
    def _command_people(args=[], botop=False, reply=reply, sender=sender):
        if len(args):
            with open(config('paths')['people']) as people_json:
                people = json.load(people_json)
            for person in people:
                if person['id'] == args[0]:
                    break
            else:
                warning('no person with id ' + str(args[0]) + ' in people.json')
                return
            can_edit = isbotop or (context == 'minecraft' and 'minecraft' in person and person['minecraft'] == sender) or (context == 'irc' and 'irc' in person and 'nicks' in person['irc'] and sender in person['irc']['nicks'])
            if len(args) >= 2:
                if args[1] == 'description':
                    if len(args) == 2:
                        reply(person.get('description', 'no description'))
                        return
                    elif can_edit:
                        person['description'] = ' '.join(args[2:])
                        with open(config('paths')['people'], 'w') as people_json:
                            json.dump(people, people_json, indent=4, separators=(',', ': '), sort_keys=True)
                        reply('description updated')
                    else:
                        warning(errors.botop)
                        return
                elif args[1] == 'name':
                    if len(args) == 2:
                        reply(person.get('name', 'no name, using id: ' + person['id']))
                    elif can_edit:
                        had_name = 'name' in person
                        person['name'] = ' '.join(args[2:])
                        with open(config('paths')['people'], 'w') as people_json:
                            json.dump(people, people_json, indent=4, separators=(',', ': '), sort_keys=True)
                        reply('name ' + ('changed' if had_name else 'added'))
                    else:
                        warning(errors.botop)
                        return
                elif args[1] == 'reddit':
                    if len(args) == 2:
                        reply(('/u/' + person['reddit']) if 'reddit' in person else 'no reddit nick')
                    elif can_edit:
                        had_reddit_nick = 'reddit' in person
                        reddit_nick = args[2][3:] if args[2].startswith('/u/') else args[2]
                        person['reddit'] = reddit_nick
                        with open(config('paths')['people'], 'w') as people_json:
                            json.dump(people, people_json, indent=4, separators=(',', ': '), sort_keys=True)
                        reply('reddit nick ' + ('changed' if had_reddit_nick else 'added'))
                    else:
                        warning(errors.botop)
                        return
                elif args[1] == 'twitter':
                    if len(args) == 2:
                        reply(('@' + person['twitter']) if 'twitter' in person else 'no twitter nick')
                        return
                    elif can_edit:
                        screen_name = args[2][1:] if args[2].startswith('@') else args[2]
                        person['twitter'] = screen_name
                        with open(config('paths')['people'], 'w') as people_json:
                            json.dump(people, people_json, indent=4, separators=(',', ': '), sort_keys=True)
                        twitter.request('lists/members/create', {'list_id': 94629160, 'screen_name': screen_name})
                        twitter.request('friendships/create', {'screen_name': screen_name})
                        reply('@' + config('twitter')['screen_name'] + ' is now following @' + screen_name)
                    else:
                        warning(errors.botop)
                        return
                elif args[1] == 'website':
                    if len(args) == 2:
                        reply(person['website'] if 'website' in person else 'no website')
                    elif can_edit:
                        had_website = 'website' in person
                        person['website'] = str(args[2])
                        with open(config('paths')['people'], 'w') as people_json:
                            json.dump(people, people_json, indent=4, separators=(',', ': '), sort_keys=True)
                        reply('website ' + ('changed' if had_website else 'added'))
                    else:
                        warning(errors.botop)
                        return
                else:
                    warning('no such people attribute: ' + str(args[1]))
                    return
            else:
                if 'name' in person:
                    reply('person with id ' + str(args[0]) + ' and name ' + person['name'])
                else:
                    reply('person with id ' + str(args[0]) + ' and no name')
        else:
            reply('http://wurstmineberg.de/people')
    
    def _command_ping(args=[], botop=False, reply=reply, sender=sender):
        if random.randrange(1024) == 0:
            reply('BWO' + 'R' * random.randint(3, 20) + 'N' * random.randing(1, 5) + 'G') # PINGCEPTION
        else:
            reply('pong')
    
    def _command_quit(args=[], botop=False, reply=reply, sender=sender):
        quitMsg = ' '.join(args) if len(args) else None
        minecraft.tellraw({
            'text': ('Shutting down the bot: ' + quitMsg) if quitMsg else 'Shutting down the bot...',
            'color': 'red'
        })
        bot.say(config('irc')['main_channel'], ('bye, ' + quitMsg) if quitMsg else random.choice(config('irc').get('quit_messages', ['bye'])))
        bot.disconnect(quitMsg if quitMsg else 'bye')
        bot.stop()
        sys.exit()
    
    def _command_raw(args=[], botop=False, reply=reply, sender=sender):
        if len(args):
            bot.send(' '.join(args))
        else:
            warning(errors.argc(1, len(args), atleast=True))
    
    def _command_restart(args=[], botop=False, reply=reply, sender=sender):
        global PREVIOUS_TOPIC
        if len(args) == 0 or (len(args) == 1 and args[0] == 'bot'):
            # restart the bot
            minecraft.tellraw({
                'text': 'Restarting the bot...',
                'color': 'red'
            })
            bot.say(config('irc')['main_channel'], random.choice(config('irc').get('quit_messages', ['brb'])))
            bot.disconnect(quitMsg if quitMsg else 'brb')
            bot.stop()
            context = newDaemonContext(pidfilename)
            stop(context)
            start(context)
            sys.exit()
        elif len(args) == 1 and args[0] == 'minecraft':
            # restart the Minecraft server
            PREVIOUS_TOPIC = (config('irc')['topic'] + ' | ' if 'topic' in config('irc') and config('irc')['topic'] is not None else '') + 'The server is restarting…'
            bot.topic(config('irc')['main_channel'], PREVIOUS_TOPIC)
            if minecraft.restart(args=args, botop=botop, reply=reply, sender=sender):
                reply('Server restarted.')
                update_topic()
            else:
                reply('Could not restart the server!')
        else:
            warning('Usage: restart [minecraft | bot]')
    
    def _command_status(args=[], botop=False, reply=reply, sender=sender):
        if minecraft.status():
            if context != 'minecraft':
                players = minecraft.online_players()
                if len(players):
                    reply('Online players: ' + ', '.join(nicksub.sub(nick, 'minecraft', context) for nick in players))
                else:
                    reply('The server is currently empty.')
            version = minecraft.version()
            if version is None:
                reply('unknown Minecraft version')
            elif reply_format == 'tellraw':
                reply({
                    'text': 'Minecraft version ',
                    'extra': [
                        {
                            'text': version,
                            'clickEvent': {
                                'action': 'open_url',
                                'value': 'http://minecraft.gamepedia.com/Version_history' + ('/Development_versions#' if 'pre' in version or version[2:3] == 'w' else '#') + version
                            }
                        }
                    ]
                })
            else:
                reply('Minecraft version ' + version)
        else:
            reply('The server is currently offline.')
    
    def _command_stop(args=[], botop=False, reply=reply, sender=sender):
        global PREVIOUS_TOPIC
        if len(args) == 0 or (len(args) == 1 and args[0] == 'bot'):
            # stop the bot
            return _command_quit(args=[], botop=botop, reply=reply, sender=sender)
        elif len(args) == 1 and args[0] == 'minecraft':
            # stop the Minecraft server
            PREVIOUS_TOPIC = (config('irc')['topic'] + ' | ' if 'topic' in config('irc') and config('irc')['topic'] is not None else '') + 'The server is down for now. Blame ' + str(sender) + '.'
            bot.topic(config('irc')['main_channel'], PREVIOUS_TOPIC)
            if minecraft.stop(args=args, botop=botop, reply=reply, sender=sender):
                reply('Server stopped.')
            else:
                warning('The server could not be stopped! D:')
        else:
            warning('Usage: stop [minecraft | bot]')
    
    def _command_time(args=[], botop=False, reply=reply, sender=sender):
        telltime(func=reply)
    
    def _command_topic(args=[], botop=False, reply=reply, sender=sender):
        if len(args):
            update_config(['irc', 'topic'], ' '.join(str(arg) for arg in args))
            update_topic()
            reply('Topic changed.')
        else:
            warning(errors.argc(1, len(args), atleast=True))
    
    def _command_tweet(args=[], botop=False, reply=reply, sender=sender):
        if len(args):
            tweet = nicksub.textsub(' '.join(args), context, 'twitter')
            if len(tweet) > 140:
                warning('too long')
            else:
                r = twitter.request('statuses/update', {'status': tweet})
                if 'id' in r.json():
                    url = 'https://twitter.com/wurstmineberg/status/' + str(r.json()['id'])
                    if context == 'minecraft':
                        minecraft.tellraw({
                            'text': '',
                            'extra': [
                                {
                                    'text': url,
                                    'color': 'gold',
                                    'clickEvent': {
                                        'action': 'open_url',
                                        'value': url
                                    }
                                }
                            ]
                        })
                    else:
                        command(None, None, 'pastetweet', [r.json()['id']], reply_format='tellraw')
                    if context == 'irc' and chan == config('irc')['main_channel']:
                        bot.say(chan, url)
                    else:
                        command(None, None, 'pastetweet', [r.json()['id']], reply=lambda msg: bot.say(config('irc')['main_channel'] if chan is None else chan, msg))
                else:
                    warning('Error ' + str(r.status_code))
        else:
            warning(errors.argc(1, len(args), atleast=True))
    
    def _command_update(args=[], botop=False, reply=reply, sender=sender):
        if len(args):
            if args[0] == 'snapshot':
                if len(args) == 2:
                    reply('updating' + ('...' if context == 'minecraft' else '…'))
                    minecraft.update(args[1], snapshot=True)
                    reply(('...' if context == 'minecraft' else '…') + 'done')
                else:
                    warning('Usage: update (snapshot <snapshot_id> | <version>)')
            elif len(args) == 1:
                reply('updating' + ('...' if context == 'minecraft' else '…'))
                minecraft.update(args[0], snapshot=False)
                reply(('...' if context == 'minecraft' else '…') + 'done')
            else:
                warning('Usage: update (snapshot <snapshot_id> | <version>)')
        else:
            warning('Usage: update (snapshot <snapshot_id> | <version>)')
    
    def _command_version(args=[], botop=False, reply=reply, sender=sender):
        reply('I am wurstminebot version ' + __version__)
    
    def _command_whitelist(args=[], botop=False, reply=reply, sender=sender):
        if len(args) in [2, 3]:
            try:
                if len(args) == 3 and args[2] is not None and len(args[2]):
                    screen_name = args[2][1:] if args[2].startswith('@') else args[2]
                else:
                    screen_name = None
                minecraft.whitelist_add(args[0], args[1])
            except ValueError:
                warning('id ' + str(args[0]) + ' already exists')
            else:
                reply(str(args[1]) + ' is now whitelisted')
                if len(args) == 3:
                    command(sender=sender, chan=chan, cmd='people', args=[args[0], 'twitter', args[2]], context=context, reply=reply, reply_format=reply_format)
        else:
            warning('Usage: whitelist <unique_id> <minecraft_name> [<twitter_username>]')
    
    commands = {
        'achievementtweet': {
            'description': 'toggle achievement message tweeting',
            'function': _command_achievementtweet,
            'usage': '[on | off [<time>]]'
        },
        'alias': {
            'description': 'add, edit, or remove an alias (you can use aliases like regular commands)',
            'function': _command_alias,
            'usage': '<alias_name> [<text>...]'
        },
        'command': {
            'botop_only': True,
            'description': 'perform Minecraft server command',
            'function': _command_command,
            'usage': '<command> [<arguments>...]'
        },
        'deathtweet': {
            'description': 'toggle death message tweeting',
            'function': _command_deathtweet,
            'usage': '[on | off [<time>]]'
        },
        'fixstatus': {
            'description': 'update the server status on the website and in the channel topic',
            'function': update_all,
            'usage': None
        },
        'lastseen': {
            'description': 'when was the player last seen logging in or out on Minecraft',
            'function': _command_lastseen,
            'usage': '<player>'
        },
        'leak': {
            'description': 'tweet the last line_count (defaults to 1) chatlog lines',
            'function': _command_leak,
            'usage': '[<line_count>]'
        },
        'mwiki': {
            'description': 'look something up in the Minecraft Wiki',
            'function': mwiki_lookup,
            'usage': '(<url> | <article>...)'
        },
        'opt': {
            'description': 'change your options',
            'function': _command_opt,
            'usage': '<option> [true|false]'
        },
        'pastemojira': {
            'description': 'print the title of a bug in Mojangs bug tracker',
            'function': _command_pastemojira,
            'usage': '(<url> | [<project_key>] <issue_id>) [nolink]'
        },
        'pastetweet': {
            'description': 'print the contents of a tweet',
            'function': _command_pastetweet,
            'usage': '(<url> | <status_id>) [nolink]'
        },
        'people': {
            'description': 'people.json management',
            'function': _command_people,
            'usage': '[<person> [<attribute> [<value>]]]'
        },
        'ping': {
            'description': 'say pong',
            'function': _command_ping,
            'usage': None
        },
        'quit': {
            'botop_only': True,
            'description': 'stop the bot with a custom quit message',
            'function': _command_quit,
            'usage': '[<quit_message>...]'
        },
        'raw': {
            'botop_only': True,
            'description': 'send raw message to IRC',
            'function': _command_raw,
            'usage': '<raw_message>...'
        },
        'restart': {
            'botop_only': True,
            'description': 'restart the Minecraft server or the bot',
            'function': _command_restart,
            'usage': '[minecraft | bot]'
        },
        'status': {
            'description': 'print some server status',
            'function': _command_status,
            'usage': None
        },
        'stop': {
            'botop_only': True,
            'description': 'stop the Minecraft server or the bot',
            'function': _command_stop,
            'usage': '[minecraft | bot]'
        },
        'time': {
            'description': 'reply with the current time',
            'function': _command_time,
            'usage': None
        },
        'topic': {
            'botop_only': True,
            'description': 'temporarily set the channel topic',
            'function': _command_topic,
            'usage': '<topic>...'
        },
        'tweet': {
            'botop_only': True,
            'description': 'tweet message',
            'function': _command_tweet,
            'usage': '<message>...'
        },
        'update': {
            'botop_only': True,
            'description': 'update Minecraft',
            'function': _command_update,
            'usage': '(snapshot <snapshot_id> | <version>)'
        },
        'version': {
            'description': 'reply with the current wurstminebot version',
            'function': _command_version,
            'usage': None
        },
        'whitelist': {
            'botop_only': True,
            'description': 'add person to whitelist',
            'function': _command_whitelist,
            'usage': '<unique_id> <minecraft_name> [<twitter_username>]'
        }
    }
    
    if cmd == 'help':
        if len(args) >= 2:
            help_text = 'Usage: help [commands | <command>]'
        elif len(args) == 0:
            help_text = 'Hello, I am wurstminebot. I sync messages between IRC and Minecraft, and respond to various commands.\nExecute “help commands” for a list of commands, or “help <command>” (replace <command> with a command name) for help on a specific command.\nTo execute a command, send it to me in private chat (here) or address me in ' + config('irc').get('main_channel', '#wurstmineberg') + ' (like this: “wurstminebot: <command>...”). You can also execute commands in a channel or in Minecraft like this: “!<command>...”.'
        elif args[0] == 'aliases':
            num_aliases = len(list(config('aliases').keys()))
            if num_aliases > 0:
                help_text = 'Currently defined aliases: ' + ', '.join(sorted(list(config('aliases').keys()))) + '. For more information, execute “help alias”.'
            else:
                help_text = 'No aliases are currently defined. For more information, execute “help alias”.'
        elif args[0] == 'commands':
            num_aliases = len(list(config('aliases').keys()))
            help_text = 'Available commands: ' + ', '.join(sorted(list(commands.keys()) + ['help'])) + (', and ' + str(num_aliases) + ' aliases.' if num_aliases > 0 else '.')
        elif args[0] == 'help':
            help_text = 'help: get help on a command\nUsage: help [commands | <command>]'
        elif args[0].lower() in commands:
            help_cmd = args[0].lower()
            help_text = help_cmd + ': ' + commands[help_cmd]['description'] + (' (requires bot op)' if commands[help_cmd].get('botop_only', False) else '') + '\nUsage: ' + help_cmd + ('' if commands[help_cmd].get('usage') is None else (' ' + commands[help_cmd]['usage']))
        else:
            help_text = errors.unknown(args[0])
        if context == 'irc':
            for line in help_text.splitlines():
                bot.say(sender, line)
        else:
            reply(sender, help_text)
    elif cmd in commands:
        isbotop = nicksub.sub(sender, context, 'irc', strict=False) in [None] + config('irc')['op_nicks']
        if isbotop or not commands[cmd].get('botop_only', False):
            return commands[cmd]['function'](args=args, botop=isbotop, reply=reply)
        else:
            warning(errors.botop)
    elif cmd in config('aliases'):
        if context != 'irc' or chan is not None:
            minecraft.tellraw({
                'text': config('aliases')[cmd],
                'color': 'gold'
            })
        bot.say((config('irc').get('main_channel', '#wurstmineberg') if sender is None else sender) if context == 'irc' and chan is None else chan, config('aliases')[cmd])
    else:
        warning(errors.unknown(cmd))

def endMOTD(sender, headers, message):
    for chan in config('irc')['channels']:
        bot.joinchan(chan)
    bot.say(config('irc')['main_channel'], "aaand I'm back.")
    minecraft.tellraw({'text': "aaand I'm back.", 'color': 'gold'})
    _debug_print("aaand I'm back.")
    update_all()
    threading.Timer(20, minecraft.update_status).start()
    InputLoop().start()

bot.bind('376', endMOTD)

def action(sender, headers, message):
    try:
        if sender == config('irc').get('nick', 'wurstminebot'):
            return
        if headers[0] == config('irc')['main_channel']:
            minecraft.tellraw({'text': '', 'extra': [{'text': '* ' + nicksub.sub(sender, 'irc', 'minecraft'), 'color': 'aqua', 'hoverEvent': {'action': 'show_text', 'value': sender + ' in ' + headers[0]}, 'clickEvent': {'action': 'suggest_command', 'value': nicksub.sub(sender, 'irc', 'minecraft') + ': '}}, {'text': ' '}, {'text': nicksub.textsub(message, 'irc', 'minecraft'), 'color': 'aqua'}]})
    except SystemExit:
        _debug_print('Exit in ACTION')
        TimeLoop.stop()
        raise
    except:
        _debug_print('Exception in ACTION:')
        if config('debug', False):
            traceback.print_exc()

bot.bind('ACTION', action)

def join(sender, headers, message):
    if len(headers):
        chan = headers[0]
    elif message is not None and len(message):
        chan = message
    else:
        return
    with open(config('paths')['people']) as people_json:
        people = json.load(people_json)
    for person in people:
        if 'minecraft' in person and command(None, None, 'opt', [person, 'sync_join_part'], context=None):
            minecraft.tellraw([
                {
                    'text': sender,
                    'color': 'yellow',
                    'clickEvent': {
                        'action': 'suggest_command',
                        'value': sender + ': '
                    }
                },
                {
                    'text': ' joined ' + chan,
                    'color': 'yellow'
                }
            ], player=person['minecraft'])

bot.bind('JOIN', join)

def nick(sender, headers, message):
    if message is None or len(message) == 0:
        return
    with open(config('paths')['people']) as people_json:
        people = json.load(people_json)
    for person in people:
        if 'minecraft' in person and command(None, None, 'opt', [person, 'sync_nick_changes'], context=None):
            minecraft.tellraw([
                {
                    'text': sender + ' is now known as ',
                    'color': 'yellow'
                },
                {
                    'text': message,
                    'color': 'yellow',
                    'clickEvent': {
                        'action': 'suggest_command',
                        'value': message + ': '
                    }
                }
            ], player=person['minecraft'])

def part(sender, headers, message):
    chans = headers[0].split(',')
    if len(chans) == 0:
        return
    elif len(chans) == 1:
        chans = chans[0]
    elif len(chans) == 2:
        chans = chans[0] + ' and ' + chans[1]
    else:
        chans = ', '.join(chans[:-1]) + ', and ' + chans[-1]
    with open(config('paths')['people']) as people_json:
        people = json.load(people_json)
    for person in people:
        if 'minecraft' in person and command(None, None, 'opt', [person, 'sync_join_part'], context=None):
            minecraft.tellraw({
                'text': sender + ' left ' + chans,
                'color': 'yellow'
            }, player=person['minecraft'])

bot.bind('PART', part)

def privmsg(sender, headers, message):
    def botsay(msg):
        for line in msg.splitlines():
            bot.say(config('irc')['main_channel'], line)
    
    try:
        _debug_print('[irc] <' + sender + '> ' + message)
        if sender == config('irc').get('nick', 'wurstminebot'):
            return
        if headers[0].startswith('#'):
            if message.startswith(config('irc').get('nick', 'wurstminebot') + ': ') or message.startswith(config('irc')['nick'] + ', '):
                cmd = message[len(config('irc').get('nick', 'wurstminebot')) + 2:].split(' ')
                if len(cmd):
                    command(sender, headers[0], cmd[0], cmd[1:], context='irc')
            elif message.startswith('!'):
                cmd = message[1:].split(' ')
                if len(cmd):
                    command(sender, headers[0], cmd[0], cmd[1:], context='irc')
            elif headers[0] == config('irc')['main_channel']:
                if re.match('https?://mojang\\.atlassian\\.net/browse/[A-Z]+-[0-9]+', message):
                    minecraft.tellraw([
                        {
                            'text': '<' + nicksub.sub(sender, 'irc', 'minecraft') + '>',
                            'color': 'aqua',
                            'hoverEvent': {
                                'action': 'show_text',
                                'value': sender + ' in ' + headers[0]
                            },
                            'clickEvent': {
                                'action': 'suggest_command',
                                'value': nicksub.sub(sender, 'irc', 'minecraft') + ': '
                            }
                        },
                        {
                            'text': ' '
                        },
                        {
                            'text': message,
                            'color': 'aqua',
                            'clickEvent': {
                                'action': 'open_url',
                                'value': message
                            }
                        }
                    ])
                    command(None, None, 'pastemojira', [message, 'nolink'], reply_format='tellraw')
                    command(sender, headers[0], 'pastemojira', [message, 'nolink'], reply=botsay)
                elif re.match('https?://twitter\\.com/[0-9A-Z_a-z]+/status/[0-9]+$', message):
                    minecraft.tellraw([
                        {
                            'text': '<' + nicksub.sub(sender, 'irc', 'minecraft') + '>',
                            'color': 'aqua',
                            'hoverEvent': {
                                'action': 'show_text',
                                'value': sender + ' in ' + headers[0]
                            },
                            'clickEvent': {
                                'action': 'suggest_command',
                                'value': nicksub.sub(sender, 'irc', 'minecraft') + ': '
                            }
                        },
                        {
                            'text': ' '
                        },
                        {
                            'text': message,
                            'color': 'aqua',
                            'clickEvent': {
                                'action': 'open_url',
                                'value': message
                            }
                        }
                    ])
                    command(None, None, 'pastetweet', [message, 'nolink'], reply_format='tellraw')
                    command(sender, headers[0], 'pastetweet', [message, 'nolink'], reply=botsay)
                else:
                    match = re.match('([a-z0-9]+:[^ ]+)(.*)$', message)
                    if match:
                        url, remaining_message = match.group(1, 2)
                        minecraft.tellraw([
                            {
                                'text': '<' + nicksub.sub(sender, 'irc', 'minecraft') + '>',
                                'color': 'aqua',
                                'hoverEvent': {
                                    'action': 'show_text',
                                    'value': sender + ' in ' + headers[0]
                                },
                                'clickEvent': {
                                    'action': 'suggest_command',
                                    'value': nicksub.sub(sender, 'irc', 'minecraft') + ': '
                                }
                            },
                            {
                                'text': ' '
                            },
                            {
                                'text': url,
                                'color': 'aqua',
                                'clickEvent': {
                                    'action': 'open_url',
                                    'value': url
                                }
                            },
                            {
                                'text': remaining_message,
                                'color': 'aqua'
                            }
                        ])
                    else:
                        minecraft.tellraw({
                            'text': '',
                            'extra': [
                                {
                                    'text': '<' + nicksub.sub(sender, 'irc', 'minecraft') + '>',
                                    'color': 'aqua',
                                    'hoverEvent': {
                                        'action': 'show_text',
                                        'value': sender + ' in ' + headers[0]
                                    },
                                    'clickEvent': {
                                        'action': 'suggest_command',
                                        'value': nicksub.sub(sender, 'irc', 'minecraft') + ': '
                                    }
                                },
                                {
                                    'text': ' '
                                },
                                {
                                    'text': nicksub.textsub(message, 'irc', 'minecraft'),
                                    'color': 'aqua'
                                }
                            ]
                        })
        else:
            cmd = message.split(' ')
            if len(cmd):
                command(sender, None, cmd[0], cmd[1:], context='irc')
    except SystemExit:
        _debug_print('Exit in PRIVMSG')
        TimeLoop.stop()
        raise
    except:
        _debug_print('Exception in PRIVMSG:')
        if config('debug', False):
            traceback.print_exc()

bot.bind('PRIVMSG', privmsg)

def run():
    bot.debugging(config('debug'))
    TimeLoop.start()
    bot.run()
    TimeLoop.stop()

def newDaemonContext(pidfilename):
    if not os.geteuid() == 0:
        sys.exit("\nOnly root can start/stop the daemon!\n")
    
    pidfile = daemon.pidlockfile.PIDLockFile(pidfilename)
    logfile = open("/opt/wurstmineberg/log/wurstminebot.log", "a")
    daemoncontext = daemon.DaemonContext(working_directory = '/opt/wurstmineberg/',
                                         pidfile = pidfile,
                                         uid = 1000, gid = 1000,
                                         stdout = logfile, stderr = logfile)
    
    daemoncontext.files_preserve = [logfile]
    daemoncontext.signal_map = {
        signal.SIGTERM: bot.stop,
        signal.SIGHUP: bot.stop,
    }
    return daemoncontext

def start(context):
    print("Starting wurstminebot version", __version__)

    if status(context.pidfile):
        print("Already running!")
        return
    else:
        # Removes the PID file
        stop(context)
    
    print("Daemonizing...")
    with context:
        print("Daemonized.")
        run()
        print("Terminating...")

def status(pidfile):
    if pidfile.is_locked():
        return os.path.exists("/proc/" + str(pidfile.read_pid()))
    return False

def stop(context):
    if status(context.pidfile):
        print("Stopping the service...")
        if context.is_open:
            context.close()
        else:
            # We don't seem to be able to stop the context so we just kill the bot
            os.kill(context.pidfile.read_pid(), signal.SIGKILL)
        try:
            context.pidfile.release()
        except lockfile.NotMyLock:
            context.pidfile.break_lock()
        
    if context.pidfile.is_locked():
        print("Service did not shutdown correctly. Cleaning up...")
        context.pidfile.break_lock()

if __name__ == '__main__':
    pidfilename = "/var/run/wurstmineberg/wurstminebot.pid"
    if arguments['start']:
        context = newDaemonContext(pidfilename)
        start(context)
    elif arguments['stop']:
        context = newDaemonContext(pidfilename)
        stop(context)
    elif arguments['restart']:
        context = newDaemonContext(pidfilename)
        stop(context)
        start(context)
    elif arguments['status']:
        pidfile = daemon.pidlockfile.PIDLockFile(pidfilename)
        print('wurstminebot ' + ('is' if status(pidfile) else 'is not') + ' running.')
    else:
        run()
