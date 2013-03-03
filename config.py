###
# Copyright (c) 2009, Mike Mueller
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

import supybot.conf as conf
import supybot.registry as registry

def configure(advanced):
    # This will be called by supybot to configure this module.  advanced is
    # a bool that specifies whether the user identified himself as an advanced
    # user or not.  You should effect your configuration by manipulating the
    # registry as appropriate.
    from supybot.questions import expect, anything, something, yn
    conf.registerPlugin('Git', True)

Git = conf.registerPlugin('Git')

conf.registerGlobalValue(Git, 'configFile',
    registry.String('git.ini', """The path to the repository configuration
        file."""))

conf.registerGlobalValue(Git, 'repoDir',
    registry.String('git_repositories', """The path where local copies of
        repositories will be kept."""))

conf.registerGlobalValue(Git, 'pollPeriod',
    registry.NonNegativeInteger(120, """The frequency (in seconds) repositories
        will be polled for changes.  Set to zero to disable polling."""))

conf.registerGlobalValue(Git, 'maxCommitsAtOnce',
    registry.NonNegativeInteger(5, """How many commits are displayed at
        once from each repository."""))

conf.registerGlobalValue(Git, 'shaSnarfing',
    registry.Boolean(True, """Look for SHAs in user messages written to the
       channel, and reply with the commit description if one is found."""))

# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
