###
# Copyright (c) 2011, Mike Mueller <mike.mueller@panopticdev.com>
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
import git
import os
import threading
import time
import traceback

API_VERSION = -1

def log_info(message):
    log.info("Git: " + message)

def log_error(message):
    log.error("Git: " + message)

def synchronized(tlockname):
    """A decorator to place an instance based lock around a method """
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

    def __init__(self, repo_dir, long_name, config_values):
        """
        Initialize with a repository with the given name and list of (name,
        value) pairs from the config section.
        """
        if API_VERSION == -1:
            raise Exception("Git-python API version uninitialized.")
        required_values = [ 'short name', 'url', 'channel' ]
        optional_values = [ 'branch', 'commit link', 'commit message' ]

        for name in required_values:
            if not filter(lambda pair: pair[0] == name, config_values):
                raise Exception('Section %s missing required value: %s' %
                        (long_name, name))

        for name, value in config_values:
            if name not in required_values and name not in optional_values:
                raise Exception('Section %s contains unrecognized value: %s' %
                        (long_name, name))

        self.lock = threading.RLock()
        self.long_name = long_name
        self.branch = 'master'
        self.commit_link = ''
        self.commit_message = '[%s|%b|%a] %m'
        self.last_commit = None
        self.repo = None
        self.errors = []

        for name, value in config_values:
            self.__dict__[name.replace(' ', '_')] = value
        self.branch = 'origin/' + self.branch

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
        except ValueError:
            return None

    @synchronized('lock')
    def get_commit_id(self, commit):
        if API_VERSION == 1:
            return commit.id
        elif API_VERSION == 3:
            return commit.hexsha
        else:
            raise Exception("Unsupported API version: %d" % API_VERSION)

    @synchronized('lock')
    def get_new_commits(self):
        if API_VERSION == 1:
            result = self.repo.commits_between(self.last_commit, self.branch)
        elif API_VERSION == 3:
            rev = "%s..%s" % (self.last_commit, self.branch)
            result = list(self.repo.iter_commits(rev))
        else:
            raise Exception("Unsupported API version: %d" % API_VERSION)
        self.last_commit = self.repo.commit(self.branch)
        return result

    @synchronized('lock')
    def get_recent_commits(self, count):
        if API_VERSION == 1:
            return self.repo.commits(start=self.branch, max_count=count)
        elif API_VERSION == 3:
            return list(self.repo.iter_commits(self.branch))[:count]
        else:
            raise Exception("Unsupported API version: %d" % API_VERSION)

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
    def format_message(self, commit):
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
            '!': '\x02',
            '%': '%',
        }
        result = []
        lines = self.commit_message.split('\n')
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
            result.append(outline)
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
    regexps = [ '_snarf' ]

    def __init__(self, irc):
        global API_VERSION
        if not git.__version__.startswith('0.'):
            raise Exception("Unsupported git-python version.")
        API_VERSION = int(git.__version__[2])
        if not API_VERSION in [1, 3]:
            log_error('git-python version %s unrecognized, using 0.3.x API.'
                    % git.__version__)
            API_VERSION = 3
        self.__parent = super(Git, self)
        self.__parent.__init__(irc)
        self.fetcher = None
        self._read_config()
        self._start_polling()

    def die(self):
        self._stop_polling()
        self.__parent.die()

    def gitrehash(self, irc, msg, args):
        """(takes no arguments)

        Reload the Git ini file and restart any period polling.
        """
        self._stop_polling()
        try:
            self._read_config()
            self._start_polling()
            irc.replySuccess()
        except Exception, e:
            irc.reply('Error reloading config: ' + str(e))

    def repolist(self, irc, msg, args, channel):
        """(takes no arguments)

        Display the names of known repositories configured for this channel.
        """
        repositories = filter(lambda r: r.channel == channel, self.repositories)
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
    repolist = wrap(repolist, ['channel'])

    def shortlog(self, irc, msg, args, channel, name, count):
        """<short name> [count]

        Display the last commits on the named repository. [count] defaults to
        1 if unspecified.
        """
        matches = filter(lambda r: r.short_name == name, self.repositories)
        if not matches:
            irc.reply('No configured repository named %s.' % name)
            return
        # Enforce a modest privacy measure... don't let people probe the
        # repository outside the designated channel.
        repository = matches[0]
        if channel != repository.channel:
            irc.reply('Sorry, not allowed in this channel.')
            return
        commits = repository.get_recent_commits(count)[::-1]
        self._display_commits(irc, repository, commits)
    shortlog = wrap(shortlog,
        ['channel', 'somethingWithoutSpaces', optional('int', 1)])

    def _display_commits(self, irc, repository, commits):
        "Display a nicely-formatted list of commits in a channel."
        commits = list(commits)
        commits_at_once = self.registryValue('maxCommitsAtOnce')
        if len(commits) > commits_at_once:
            irc.queueMsg(ircmsgs.privmsg(repository.channel,
                         "Showing latest %d of %d commits to %s..." % (
                commits_at_once,
                len(commits),
                repository.long_name,
            )))
        for commit in commits[-commits_at_once:]:
            lines = repository.format_message(commit)
            for line in lines:
                msg = ircmsgs.privmsg(repository.channel, line)
                irc.queueMsg(msg)

    def _poll(self):
        # Note that polling happens in two steps:
        #
        # 1. The GitFetcher class, running its own poll loop, fetches
        #    repositories to keep the local copies up to date.
        # 2. This _poll occurs, and looks for new commits in those local
        #    copies.  (Therefore this function should be quick. If it is
        #    slow, it may block the entire bot.)
        for repository in self.repositories:
            # Find the channel among IRC connections (first found will win)
            ircs = [irc for irc in world.ircs
                    if repository.channel in irc.state.channels]
            if not ircs:
                log_info("Skipping %s: not in channel: %s"
                    % (repository.long_name, repository.channel))
                continue
            irc = ircs[0]

            # Manual non-blocking lock calls here to avoid potentially long
            # waits (if it fails, hope for better luck in the next _poll).
            if repository.lock.acquire(blocking=False):
                try:
                    errors = repository.get_errors()
                    for e in errors:
                        log_error('Unable to fetch %s: %s' %
                            (repository.long_name, str(e)))
                    commits = repository.get_new_commits()
                    self._display_commits(irc, repository, commits)
                except Exception, e:
                    log_error('Exception in _poll repository %s: %s' %
                            (repository.short_name, str(e)))
                finally:
                    repository.lock.release()
            else:
                log.info('Unable to check repository %s: Locked.' %
                    repository.long_name)
        for irc, channel, text in messages:
            irc.queueMsg(ircmsgs.privmsg(channel, text))
        new_period = self.registryValue('pollPeriod')
        if new_period != self.poll_period:
            _stop_polling()
            _start_polling()

    def _read_config(self):
        self.repositories = []
        repo_dir = self.registryValue('repoDir')
        parser = ConfigParser.RawConfigParser()
        parser.read(self.registryValue('configFile'))
        for section in parser.sections():
            self.repositories.append(
                Repository(repo_dir, section, parser.items(section)))

    def _snarf(self, irc, msg, match):
        r"""\b(?P<sha>[0-9a-f]{6,40})\b"""
        sha = match.group('sha')
        channel = msg.args[0]
        repositories = filter(lambda r: r.channel == channel, self.repositories)
        for repository in repositories:
            commit = repository.get_commit(sha)
            if commit:
                self._display_commits(irc, repository, [commit])
                break

    def _start_polling(self):
        self.poll_period = self.registryValue('pollPeriod')
        if self.poll_period:
            self.fetcher = GitFetcher(self.repositories, self.poll_period)
            self.fetcher.start()
            schedule.addPeriodicEvent(
                self._poll, self.poll_period, now=False, name=self.name())

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
    # timeout on its own in most cases, but it would suck if it hung forever.

    def __init__(self, repositories, period, *args, **kwargs):
        """
        Takes a list of repositories and a period (in seconds) to poll them.
        As long as it is running, the repositories will be kept up to date
        every period seconds (with a git fetch).
        """
        super(GitFetcher, self).__init__(*args, **kwargs)
        self.repositories = repositories
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
        # Wait for half the period to stagger this thread and the main thread
        # and avoid lock contention.
        time.sleep(self.period/2)
        while not self.shutdown:
            end_time = time.time() + self.period
            # Poll now
            for repository in self.repositories:
                if self.shutdown: break
                try:
                    repository.fetch()
                except Exception, e:
                    repository.record_error(e)
            # Wait for the next periodic check
            while not self.shutdown and time.time() < end_time:
                time.sleep(GitFetcher.SHUTDOWN_CHECK_PERIOD)

Class = Git

# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
