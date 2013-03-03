###
# Copyright (c) 2011-2012, Mike Mueller <mike.mueller@panopticdev.com>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Do whatever you want
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
###

"""
A Supybot plugin that monitors and interacts with git repositories.
"""

import supybot.utils as utils
from supybot.commands import *
import supybot.plugins as plugins
import supybot.ircmsgs as ircmsgs
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
import supybot.schedule as schedule
import supybot.log as log
import supybot.world as world

import ConfigParser
from functools import wraps
import os
import threading
import time
import traceback

# 'import git' is performed during plugin initialization.
#
# The GitPython library has different APIs depending on the version installed.
# (0.1.x, 0.3.x supported)
GIT_API_VERSION = -1

def log_info(message):
    log.info("Git: " + message)

def log_warning(message):
    log.warning("Git: " + message)

def log_error(message):
    log.error("Git: " + message)

def plural(count, singular, plural=None):
    if count == 1:
        return singular
    if plural:
        return plural
    if singular[-1] == 's':
        return singular + 'es'
    if singular[-1] == 'y':
        return singular[:-1] + 'ies'
    return singular + 's'

def synchronized(tlockname):
    """
    Decorates a class method (with self as the first parameter) to acquire the
    member variable lock with the given name (e.g. 'lock' ==> self.lock) for
    the duration of the function (blocking).
    """
    def _synched(func):
        @wraps(func)
        def _synchronizer(self, *args, **kwargs):
            tlock = self.__getattribute__(tlockname)
            tlock.acquire()
            try:
                return func(self, *args, **kwargs)
            finally:
                tlock.release()
        return _synchronizer
    return _synched

class Repository(object):
    "Represents a git repository being monitored."

    def __init__(self, repo_dir, long_name, options):
        """
        Initialize with a repository with the given name and dict of options
        from the config section.
        """
        if GIT_API_VERSION == -1:
            raise Exception("Git-python API version uninitialized.")

        # Validate configuration ("channel" allowed for backward compatibility)
        required_values = ['short name', 'url']
        optional_values = ['branch', 'channel', 'channels', 'commit link',
                           'commit message', 'commit reply']
        for name in required_values:
            if name not in options:
                raise Exception('Section %s missing required value: %s' %
                        (long_name, name))
        for name, value in options.items():
            if name not in required_values and name not in optional_values:
                raise Exception('Section %s contains unrecognized value: %s' %
                        (long_name, name))

        # Initialize
        self.branch = 'origin/' + options.get('branch', 'master')
        self.channels = options.get('channels', options.get('channel')).split()
        self.commit_link = options.get('commit link', '')
        self.commit_message = options.get('commit message', '[%s|%b|%a] %m')
        self.commit_reply = options.get('commit reply', '')
        self.errors = []
        self.last_commit = None
        self.lock = threading.RLock()
        self.long_name = long_name
        self.short_name = options['short name']
        self.repo = None
        self.url = options['url']

        if not os.path.exists(repo_dir):
            os.makedirs(repo_dir)
        self.path = os.path.join(repo_dir, self.short_name)

        # TODO: Move this to GitWatcher (separate thread)
        self.clone()

    @synchronized('lock')
    def clone(self):
        "If the repository doesn't exist on disk, clone it."
        if not os.path.exists(self.path):
            git.Git('.').clone(self.url, self.path, no_checkout=True)
        self.repo = git.Repo(self.path)
        self.last_commit = self.repo.commit(self.branch)

    @synchronized('lock')
    def fetch(self):
        "Contact git repository and update last_commit appropriately."
        self.repo.git.fetch()

    @synchronized('lock')
    def get_commit(self, sha):
        "Fetch the commit with the given SHA.  Returns None if not found."
        try:
            return self.repo.commit(sha)
        except ValueError: # 0.1.x
            return None
        except git.GitCommandError: # 0.3.x
            return None

    @synchronized('lock')
    def get_commit_id(self, commit):
        if GIT_API_VERSION == 1:
            return commit.id
        elif GIT_API_VERSION == 3:
            return commit.hexsha
        else:
            raise Exception("Unsupported API version: %d" % GIT_API_VERSION)

    @synchronized('lock')
    def get_new_commits(self):
        if GIT_API_VERSION == 1:
            result = self.repo.commits_between(self.last_commit, self.branch)
        elif GIT_API_VERSION == 3:
            rev = "%s..%s" % (self.last_commit, self.branch)
            # Workaround for GitPython bug:
            # https://github.com/gitpython-developers/GitPython/issues/61
            self.repo.odb.update_cache()
            result = self.repo.iter_commits(rev)
        else:
            raise Exception("Unsupported API version: %d" % GIT_API_VERSION)
        self.last_commit = self.repo.commit(self.branch)
        return list(result)

    @synchronized('lock')
    def get_recent_commits(self, count):
        if GIT_API_VERSION == 1:
            return self.repo.commits(start=self.branch, max_count=count)
        elif GIT_API_VERSION == 3:
            return list(self.repo.iter_commits(self.branch))[:count]
        else:
            raise Exception("Unsupported API version: %d" % GIT_API_VERSION)

    @synchronized('lock')
    def format_link(self, commit):
        "Return a link to view a given commit, based on config setting."
        result = ''
        escaped = False
        for c in self.commit_link:
            if escaped:
                if c == 'c':
                    result += self.get_commit_id(commit)[0:7]
                elif c == 'C':
                    result += self.get_commit_id(commit)
                else:
                    result += c
                escaped = False
            elif c == '%':
                escaped = True
            else:
                result += c
        return result

    @synchronized('lock')
    def format_message(self, commit, format_str=None):
        """
        Generate an formatted message for IRC from the given commit, using
        the format specified in the config. Returns a list of strings.
        """
        MODE_NORMAL = 0
        MODE_SUBST = 1
        MODE_COLOR = 2
        subst = {
            'a': commit.author.name,
            'b': self.branch[self.branch.rfind('/')+1:],
            'c': self.get_commit_id(commit)[0:7],
            'C': self.get_commit_id(commit),
            'e': commit.author.email,
            'l': self.format_link(commit),
            'm': commit.message.split('\n')[0],
            'n': self.long_name,
            's': self.short_name,
            'u': self.url,
            'r': '\x0f',
            '!': '\x02',
            '%': '%',
        }
        result = []
        if not format_str:
            format_str = self.commit_message
        lines = format_str.split('\n')
        for line in lines:
            mode = MODE_NORMAL
            outline = ''
            for c in line:
                if mode == MODE_SUBST:
                    if c in subst.keys():
                        outline += subst[c]
                        mode = MODE_NORMAL
                    elif c == '(':
                        color = ''
                        mode = MODE_COLOR
                    else:
                        outline += c
                        mode = MODE_NORMAL
                elif mode == MODE_COLOR:
                    if c == ')':
                        outline += '\x03' + color
                        mode = MODE_NORMAL
                    else:
                        color += c
                elif c == '%':
                    mode = MODE_SUBST
                else:
                    outline += c
            result.append(outline.encode('utf-8'))
        return result

    @synchronized('lock')
    def record_error(self, e):
        "Save the exception 'e' for future error reporting."
        self.errors.append(e)

    @synchronized('lock')
    def get_errors(self):
        "Return a list of exceptions that have occurred since last get_errors."
        result = self.errors
        self.errors = []
        return result

class Git(callbacks.PluginRegexp):
    "Please see the README file to configure and use this plugin."

    threaded = True
    unaddressedRegexps = [ '_snarf' ]

    def __init__(self, irc):
        self.init_git_python()
        self.__parent = super(Git, self)
        self.__parent.__init__(irc)
        # Workaround the fact that self.log already exists in plugins
        self.log = LogWrapper(self.log, Git._log.__get__(self))
        self.fetcher = None
        self._stop_polling()
        try:
            self._read_config()
        except Exception, e:
            if 'reply' in dir(irc):
                irc.reply('Warning: %s' % str(e))
            else:
                # During bot startup, there is no one to reply to.
                log_warning(str(e))
        self._schedule_next_event()

    def init_git_python(self):
        global GIT_API_VERSION, git
        try:
            import git
        except ImportError:
            raise Exception("GitPython is not installed.")
        if not git.__version__.startswith('0.'):
            raise Exception("Unsupported GitPython version.")
        GIT_API_VERSION = int(git.__version__[2])
        if not GIT_API_VERSION in [1, 3]:
            log_error('GitPython version %s unrecognized, using 0.3.x API.'
                    % git.__version__)
            GIT_API_VERSION = 3

    def die(self):
        self._stop_polling()
        self.__parent.die()

    def _log(self, irc, msg, args, channel, name, count):
        """<short name> [count]

        Display the last commits on the named repository. [count] defaults to
        1 if unspecified.
        """
        matches = filter(lambda r: r.short_name == name, self.repository_list)
        if not matches:
            irc.reply('No configured repository named %s.' % name)
            return
        # Enforce a modest privacy measure... don't let people probe the
        # repository outside the designated channel.
        repository = matches[0]
        if channel not in repository.channels:
            irc.reply('Sorry, not allowed in this channel.')
            return
        commits = repository.get_recent_commits(count)[::-1]
        self._reply_commits(irc, channel, repository, commits)
    _log = wrap(_log, ['channel', 'somethingWithoutSpaces',
                       optional('positiveInt', 1)])

    def rehash(self, irc, msg, args):
        """(takes no arguments)

        Reload the Git ini file and restart any period polling.
        """
        self._stop_polling()
        try:
            self._read_config()
            self._schedule_next_event()
            n = len(self.repository_list)
            irc.reply('Git reinitialized with %d %s.' %
                      (n, plural(n, 'repository')))
        except Exception, e:
            irc.reply('Warning: %s' % str(e))

    def repositories(self, irc, msg, args, channel):
        """(takes no arguments)

        Display the names of known repositories configured for this channel.
        """
        repositories = filter(lambda r: channel in r.channels,
                              self.repository_list)
        if not repositories:
            irc.reply('No repositories configured for this channel.')
            return
        for r in repositories:
            fmt = '\x02%(short_name)s\x02 (%(name)s, branch: %(branch)s)'
            irc.reply(fmt % {
                'branch': r.branch.split('/')[-1],
                'name': r.long_name,
                'short_name': r.short_name,
                'url': r.url,
            })
    repositories = wrap(repositories, ['channel'])

    def gitrehash(self, irc, msg, args):
        "Obsolete command, remove this function eventually."
        irc.reply('"gitrehash" is obsolete, please use "rehash".')

    def repolist(self, irc, msg, args):
        "Obsolete command, remove this function eventually."
        irc.reply('"repolist" is obsolete, please use "repositories".')

    def shortlog(self, irc, msg, args):
        "Obsolete command, remove this function eventually."
        irc.reply('"shortlog" is obsolete, please use "log".')

    # Overridden to hide the obsolete commands
    def listCommands(self, pluginCommands=[]):
        return ['log', 'rehash', 'repositories']

    def _display_commits(self, irc, channel, repository, commits):
        "Display a nicely-formatted list of commits in a channel."
        commits = list(commits)
        commits_at_once = self.registryValue('maxCommitsAtOnce')
        if len(commits) > commits_at_once:
            irc.queueMsg(ircmsgs.privmsg(channel,
                         "Showing latest %d of %d commits to %s..." %
                         (commits_at_once, len(commits), repository.long_name)))
        for commit in commits[-commits_at_once:]:
            lines = repository.format_message(commit)
            for line in lines:
                msg = ircmsgs.privmsg(channel, line)
                irc.queueMsg(msg)

    # Post commits to channel as a reply
    def _reply_commits(self, irc, channel, repository, commits):
        commits = list(commits)
        commits_at_once = self.registryValue('maxCommitsAtOnce')
        if len(commits) > commits_at_once:
            irc.reply("Showing latest %d of %d commits to %s..." %
                      (commits_at_once, len(commits), repository.long_name))
        format_str = repository.commit_reply or repository.commit_message
        for commit in commits[-commits_at_once:]:
            lines = repository.format_message(commit, format_str)
            map(irc.reply, lines)

    def _poll(self):
        # Note that polling happens in two steps:
        #
        # 1. The GitFetcher class, running its own poll loop, fetches
        #    repositories to keep the local copies up to date.
        # 2. This _poll occurs, and looks for new commits in those local
        #    copies.  (Therefore this function should be quick. If it is
        #    slow, it may block the entire bot.)
        try:
            for repository in self.repository_list:
                # Find the IRC/channel pairs to notify
                targets = []
                for irc in world.ircs:
                    for channel in repository.channels:
                        if channel in irc.state.channels:
                            targets.append((irc, channel))
                if not targets:
                    log_info("Skipping %s: not in configured channel(s)." %
                             repository.long_name)
                    continue

                # Manual non-blocking lock calls here to avoid potentially long
                # waits (if it fails, hope for better luck in the next _poll).
                if repository.lock.acquire(blocking=False):
                    try:
                        errors = repository.get_errors()
                        for e in errors:
                            log_error('Unable to fetch %s: %s' %
                                (repository.long_name, str(e)))
                        commits = repository.get_new_commits()[::-1]
                        for irc, channel in targets:
                            self._display_commits(irc, channel, repository,
                                                  commits)
                    except Exception, e:
                        log_error('Exception in _poll repository %s: %s' %
                                (repository.short_name, str(e)))
                    finally:
                        repository.lock.release()
                else:
                    log.info('Postponing repository read: %s: Locked.' %
                        repository.long_name)
            self._schedule_next_event()
        except Exception, e:
            log_error('Exception in _poll(): %s' % str(e))
            traceback.print_exc(e)

    def _read_config(self):
        self.repository_list = []
        repo_dir = self.registryValue('repoDir')
        config = self.registryValue('configFile')
        if not os.access(config, os.R_OK):
            raise Exception('Cannot access configuration file: %s' % config)
        parser = ConfigParser.RawConfigParser()
        parser.read(config)
        for section in parser.sections():
            options = dict(parser.items(section))
            self.repository_list.append(Repository(repo_dir, section, options))

    def _schedule_next_event(self):
        period = self.registryValue('pollPeriod')
        if period > 0:
            if not self.fetcher or not self.fetcher.isAlive():
                self.fetcher = GitFetcher(self.repository_list, period)
                self.fetcher.start()
            schedule.addEvent(self._poll, time.time() + period,
                              name=self.name())
        else:
            self._stop_polling()

    def _snarf(self, irc, msg, match):
        r"""\b(?P<sha>[0-9a-f]{6,40})\b"""
        if self.registryValue('shaSnarfing'):
            sha = match.group('sha')
            channel = msg.args[0]
            repositories = filter(lambda r: channel in r.channels,
                                  self.repository_list)
            for repository in repositories:
                commit = repository.get_commit(sha)
                if commit:
                    self._reply_commits(irc, channel, repository, [commit])
                    break

    def _stop_polling(self):
        # Never allow an exception to propagate since this is called in die()
        if self.fetcher:
            try:
                self.fetcher.stop()
                self.fetcher.join() # This might take time, but it's safest.
            except Exception, e:
                log_error('Stopping fetcher: %s' % str(e))
            self.fetcher = None
        try:
            schedule.removeEvent(self.name())
        except KeyError:
            pass
        except Exception, e:
            log_error('Stopping scheduled task: %s' % str(e))

class GitFetcher(threading.Thread):
    "A thread object to perform long-running Git operations."

    # I don't know of any way to shut down a thread except to have it
    # check a variable very frequently.
    SHUTDOWN_CHECK_PERIOD = 0.1 # Seconds

    # TODO: Wrap git fetch command and enforce a timeout.  Git will probably
    # timeout on its own in most cases, but I have actually seen it hang
    # forever on "fetch" before.

    def __init__(self, repositories, period, *args, **kwargs):
        """
        Takes a list of repositories and a period (in seconds) to poll them.
        As long as it is running, the repositories will be kept up to date
        every period seconds (with a git fetch).
        """
        super(GitFetcher, self).__init__(*args, **kwargs)
        self.repository_list = repositories
        self.period = period * 1.1 # Hacky attempt to avoid resonance
        self.shutdown = False

    def stop(self):
        """
        Shut down the thread as soon as possible. May take some time if
        inside a long-running fetch operation.
        """
        self.shutdown = True

    def run(self):
        "The main thread method."
        # Initially wait for half the period to stagger this thread and
        # the main thread and avoid lock contention.
        end_time = time.time() + self.period/2
        while not self.shutdown:
            try:
                for repository in self.repository_list:
                    if self.shutdown: break
                    if repository.lock.acquire(blocking=False):
                        try:
                            repository.fetch()
                        except Exception, e:
                            repository.record_error(e)
                        finally:
                            repository.lock.release()
                    else:
                        log_info('Postponing repository fetch: %s: Locked.' %
                                 repository.long_name)
            except Exception, e:
                log_error('Exception checking repository %s: %s' %
                          (repository.short_name, str(e)))
            # Wait for the next periodic check
            while not self.shutdown and time.time() < end_time:
                time.sleep(GitFetcher.SHUTDOWN_CHECK_PERIOD)
            end_time = time.time() + self.period

class LogWrapper(object):
    """
    Horrific workaround for the fact that PluginMixin has a member variable
    called 'log' -- wiping out my 'log' command.  Delegates all requests to
    the log, and when called as a function, performs the log command.
    """

    LOGGER_METHODS = [
        'debug',
        'info',
        'warning',
        'error',
        'critical',
        'exception',
    ]

    def __init__(self, log_object, log_command):
        "Construct the wrapper with the objects being wrapped."
        self.log_object = log_object
        self.log_command = log_command
        self.__doc__ = log_command.__doc__

    def __call__(self, *args, **kwargs):
        return self.log_command(*args, **kwargs)

    def __getattr__(self, name):
        if name in LogWrapper.LOGGER_METHODS:
            return getattr(self.log_object, name)
        else:
            return getattr(self.log_command, name)

# Because isCommandMethod() relies on inspection (whyyyy), I do this (gross)
import inspect
if 'git_orig_ismethod' not in dir(inspect):
    inspect.git_orig_ismethod = inspect.ismethod
    inspect.ismethod = \
        lambda x: type(x) == LogWrapper or inspect.git_orig_ismethod(x)

Class = Git

# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
