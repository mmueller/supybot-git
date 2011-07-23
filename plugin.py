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
import git
import os
import subprocess
import traceback

class Repository:
    "Represents a git repository being monitored."

    def __init__(self, repo_dir, long_name, config_values):
        """
        Initialize with a repository with the given name and list of (name,
        value) pairs from the config section.
        """
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

        self.long_name = long_name
        self.branch = 'master'
        self.commit_link = ''
        self.commit_message = '[%s|%b|%a] %m'
        self.last_commit = None
        self.repo = None

        for name, value in config_values:
            self.__dict__[name.replace(' ', '_')] = value
        self.branch = 'origin/' + self.branch

        if not os.path.exists(repo_dir):
            os.makedirs(repo_dir)
        self.path = os.path.join(repo_dir, self.short_name)

        self.clone()

    def clone(self):
        "If the repository doesn't exist on disk, clone it."
        if not os.path.exists(self.path):
            git.Git('.').clone(self.url, self.path, no_checkout=True)
        self.repo = git.Repo(self.path)
        self.last_commit = self.repo.commit(self.branch)

    def fetch(self):
        "Contact git repository and update last_commit appropriately."
        self.repo.git.fetch()

    def get_new_commits(self):
        result = self.repo.commits_between(self.last_commit, self.branch)
        self.last_commit = self.repo.commit(self.branch)
        return result

    def get_recent_commits(self, count):
        return self.repo.commits(start=self.branch, max_count=count)

    def format_link(self, commit):
        "Return a link to view a given commit, based on config setting."
        result = ''
        escaped = False
        for c in self.commit_link:
            if escaped:
                if c == 'c':
                    result += commit.id[0:7]
                elif c == 'C':
                    result += commit.id
                else:
                    result += c
                escaped = False
            elif c == '%':
                escaped = True
            else:
                result += c
        return result

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
            'c': commit.id[0:7],
            'C': commit.id,
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

class Git(callbacks.Plugin):
    "Please see the README file to configure and use this plugin."
    threaded = True

    def __init__(self, irc):
        self.__parent = super(Git, self)
        self.__parent.__init__(irc)
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
            irc.noReply()
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
        commits = repository.get_recent_commits(count)
        self._display_commits(irc, repository, commits)
    shortlog = wrap(shortlog,
        ['channel', 'somethingWithoutSpaces', optional('int', 1)])

    def _display_commits(self, irc, repository, commits):
        "Display a nicely-formatted list of commits in a channel."
        for commit in commits:
            lines = repository.format_message(commit)
            for line in lines:
                print line
                msg = ircmsgs.privmsg(repository.channel, line)
                irc.queueMsg(msg)

    def _error(self, message):
        log.error("Git: " + message)

    def _poll(self):
        for repository in self.repositories:
            # Find the channel among IRC connections (first found will win)
            ircs = [irc for irc in world.ircs
                    if repository.channel in irc.state.channels]
            if not ircs:
                self._error("Can't find channel: " + repository.channel)
                return
            irc = ircs[0]

            try:
                repository.fetch()
                commits = repository.get_new_commits()
                self._display_commits(irc, repository, commits)
            except Exception, e:
                self._error('Exception polling repository %s: %s' %
                        (repository.short_name, str(e)))
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

    def _start_polling(self):
        self.poll_period = self.registryValue('pollPeriod')
        if self.poll_period:
            schedule.addPeriodicEvent(
                self._poll, self.poll_period, now=False, name=self.name())

    def _stop_polling(self):
        if self.poll_period:
            schedule.removeEvent(self.name())

Class = Git

# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
