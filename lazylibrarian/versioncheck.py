#  This file is part of Lazylibrarian.
#
#  Lazylibrarian is free software':'you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.

import os
import platform
import re
import time
import socket
import subprocess
import tarfile
import threading
import urllib2

import lazylibrarian
import lib.simplejson as simplejson
from lazylibrarian import logger, version
from lazylibrarian.common import USER_AGENT, internet


#
# Function to execute GIT commands taking care of error logging etc


def runGit(args):

    if lazylibrarian.CONFIG['GIT_PROGRAM']:
        git_locations = ['"' + lazylibrarian.CONFIG['GIT_PROGRAM'] + '"']
    else:
        git_locations = ['git']

    if platform.system().lower() == 'darwin':
        git_locations.append('/usr/local/git/bin/git')

    output = err = None

    for cur_git in git_locations:

        cmd = cur_git + ' ' + args

        try:
            logger.debug('(RunGit)Trying to execute: "' + cmd + '" with shell in ' + lazylibrarian.PROG_DIR)
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 shell=True, cwd=lazylibrarian.PROG_DIR)
            output, err = p.communicate()
            logger.debug('(RunGit)Git output: [%s]' % output.strip('\n'))

        except OSError:
            logger.debug('(RunGit)Command ' + cmd + ' didn\'t work, couldn\'t find git')
            continue

        if 'not found' in output or "not recognized as an internal or external command" in output:
            logger.debug('(RunGit)Unable to find git with command ' + cmd)
            logger.error('git not found - please ensure git executable is in your PATH')
            output = None
        elif 'fatal:' in output or err:
            logger.error('(RunGit)Git returned bad info. Are you sure this is a git installation?')
            output = None
        elif output:
            break

    return output, err

#
# function to determine what type of install we are on & sets current Branch value
# - windows
# - git based
# - deployed source code


def getInstallType():
    # need a way of detecting if we are running a windows .exe file
    # (which we can't upgrade)  rather than just running git or source on windows
    # We use a string in the version.py file for this
    # FUTURE:   Add a version number string in this file too?
    try:
        install = version.LAZYLIBRARIAN_VERSION.lower()
    except:
        install = 'unknown'

    if install in ['windows', 'win32build']:
        lazylibrarian.CONFIG['INSTALL_TYPE'] = 'win'
        lazylibrarian.CURRENT_BRANCH = 'Windows'

    elif install == 'package':  # deb, rpm, other non-upgradeable
        lazylibrarian.CONFIG['INSTALL_TYPE'] = 'package'
        lazylibrarian.CONFIG['GIT_BRANCH'] = 'Package'

    elif os.path.isdir(os.path.join(lazylibrarian.PROG_DIR, '.git')):
        lazylibrarian.CONFIG['INSTALL_TYPE'] = 'git'
        lazylibrarian.CONFIG['GIT_BRANCH'] = getCurrentGitBranch()
    else:
        lazylibrarian.CONFIG['INSTALL_TYPE'] = 'source'
        lazylibrarian.CONFIG['GIT_BRANCH'] = 'master'

    logger.debug('(getInstallType) [%s] install detected. Setting Branch to [%s]' %
                 (lazylibrarian.CONFIG['INSTALL_TYPE'], lazylibrarian.CONFIG['GIT_BRANCH']))

#
# Establish the version of the installed app for Source or GIT only
# Global variable set in LazyLibrarian.py on startup as it should be


def getCurrentVersion():

    if lazylibrarian.CONFIG['INSTALL_TYPE'] == 'win':
        logger.debug('(getCurrentVersion) Windows install - no update available')

        # Don't have a way to update exe yet, but don't want to set VERSION to None
        VERSION = 'Windows Install'

    elif lazylibrarian.CONFIG['INSTALL_TYPE'] == 'git':
        output, err = runGit('rev-parse HEAD')

        if not output:
            logger.error('(getCurrentVersion) Couldn\'t find latest git installed version.')
            cur_commit_hash = 'GIT Cannot establish version'
        else:
            cur_commit_hash = output.strip()

            if not re.match('^[a-z0-9]+$', cur_commit_hash):
                logger.error('(getCurrentVersion) Output doesn\'t look like a hash, not using it')
                cur_commit_hash = 'GIT invalid hash return'

        VERSION = cur_commit_hash

    elif lazylibrarian.CONFIG['INSTALL_TYPE'] in ['source', 'package']:

        version_file = os.path.join(lazylibrarian.PROG_DIR, 'version.txt')

        if not os.path.isfile(version_file):
            VERSION = 'No Version File'
            logger.debug('(getCurrentVersion) [%s] missing.' % version_file)
            return VERSION
        else:
            fp = open(version_file, 'r')
            current_version = fp.read().strip(' \n\r')
            fp.close()

            if current_version:
                VERSION = current_version
            else:
                VERSION = 'No Version set in file'
                return VERSION
    else:
        logger.error('(getCurrentVersion) Install Type not set - cannot get version value')
        VERSION = 'Install type not set'
        return VERSION

    updated = updateVersionFile(VERSION)
    if updated:
        logger.debug('(getCurrentVersion) - Install type [%s] Local Version is set to [%s] ' % (
                 lazylibrarian.CONFIG['INSTALL_TYPE'], VERSION))
    else:
        logger.debug('(getCurrentVersion) - Install type [%s] Local Version is unchanged [%s] ' % (
                 lazylibrarian.CONFIG['INSTALL_TYPE'], VERSION))

    return VERSION

#
# Returns current branch name of installed version from GIT
# return "NON GIT INSTALL" if INSTALL TYPE is not GIT


def getCurrentGitBranch():
    # Can only work for GIT driven installs, so check install type
    if lazylibrarian.CONFIG['INSTALL_TYPE'] != 'git':
        logger.debug('Non GIT Install doing getCurrentGitBranch')
        return 'NON GIT INSTALL'

    # use git rev-parse --abbrev-ref HEAD which returns the name of the current branch
    current_branch, err = runGit('rev-parse --abbrev-ref HEAD')
    current_branch = str(current_branch)
    current_branch = current_branch.strip(' \n\r')

    if not current_branch:
        logger.error('failed to return current branch value')
        return 'InvalidBranch'

    logger.debug('(getCurrentGitBranch) Current local branch of repo is [%s] ' % current_branch)

    return current_branch


def checkForUpdates():
    """ Called at startup, from webserver with thread name WEBSERVER, or as a cron job """
    if 'Thread-' in threading.currentThread().name:
        threading.currentThread().name = "CRON-VERSIONCHECK"
    logger.debug('Set Install Type, Current & Latest Version and Commit status')
    if internet():
        getInstallType()
        lazylibrarian.CONFIG['CURRENT_VERSION'] = getCurrentVersion()
        lazylibrarian.CONFIG['LATEST_VERSION'] = getLatestVersion()
        if lazylibrarian.CONFIG['CURRENT_VERSION'] == lazylibrarian.CONFIG['LATEST_VERSION']:
            lazylibrarian.CONFIG['COMMITS_BEHIND'] = 0
            lazylibrarian.COMMIT_LIST = ""
        else:
            lazylibrarian.CONFIG['COMMITS_BEHIND'], lazylibrarian.COMMIT_LIST = getCommitDifferenceFromGit()
        logger.debug('Update check complete')
    else:
        logger.warn('No internet connection')

# Return latest version from GITHUB
# if GIT install return latest on current branch
# if nonGIT install return latest from master


def getLatestVersion():
    # Can only work for GIT driven installs, so check install type
    #lazylibrarian.CONFIG['COMMITS_BEHIND'] = 'Unknown'

    if lazylibrarian.CONFIG['INSTALL_TYPE'] in ['git', 'source', 'package']:
        latest_version = getLatestVersion_FromGit()
    elif lazylibrarian.CONFIG['INSTALL_TYPE'] in ['win']:
        latest_version = 'WINDOWS INSTALL'
    else:
        latest_version = 'UNKNOWN INSTALL'

    lazylibrarian.CONFIG['LATEST_VERSION'] = latest_version
    return latest_version


# Don't call directly, use getLatestVersion as wrapper.
# Also removed reference to global variable setting.
def getLatestVersion_FromGit():
    latest_version = 'Unknown'

    # Can only work for non Windows driven installs, so check install type
    if lazylibrarian.CONFIG['INSTALL_TYPE'] == 'win':
        logger.debug(
            '(getLatestVersion_FromGit) Code Error - Windows install - should not be called under a windows install')
        latest_version = 'WINDOWS INSTALL'
    else:
        # check current branch value of the local git repo as folks may pull from a branch not master
        branch = lazylibrarian.CONFIG['GIT_BRANCH']

        if branch == 'InvalidBranch':
            logger.debug('(getLatestVersion_FromGit) - Failed to get a valid branch name from local repo')
        else:
            if branch == 'Package':  # check packages against master
                branch = 'master'
            # Get the latest commit available from github
            url = 'https://api.github.com/repos/%s/%s/commits/%s' % (
                lazylibrarian.CONFIG['GIT_USER'], lazylibrarian.CONFIG['GIT_REPO'], branch)
            logger.debug(
                '(getLatestVersion_FromGit) Retrieving latest version information from github command=[%s]' % url)

            age = lazylibrarian.CONFIG['GIT_UPDATED']
            try:
                request = urllib2.Request(url)
                request.add_header('User-Agent', USER_AGENT)
                if age:
                    logger.debug('(getLatestVersion_FromGit) Checking if modified since %s' % age)
                    request.add_header('If-Modified-Since', age)
                resp = urllib2.urlopen(request, timeout=30)
                result = resp.read()
                git = simplejson.JSONDecoder().decode(result)
                latest_version = git['sha']
                logger.debug('(getLatestVersion_FromGit) Branch [%s] Latest Version has been set to [%s]' % (
                             branch, latest_version))
            except Exception as e:
                if hasattr(e, 'reason'):
                    errmsg = e.reason
                else:
                    errmsg = str(e)

                if hasattr(e, 'code') and str(e.code) == '304':  # Not modified
                    latest_version = lazylibrarian.CONFIG['CURRENT_VERSION']
                    logger.debug('(getLatestVersion_FromGit) Not modified, currently on Latest Version')
                else:
                    logger.warn('(getLatestVersion_FromGit) Could not get the latest commit from github')
                    logger.debug('git error for %s: %s' % (url, errmsg))
                    latest_version = 'Not_Available_From_GitHUB'

    return latest_version


def getCommitDifferenceFromGit():
    # See how many commits behind we are
    commits = -1
    # Takes current latest version value and trys to diff it with the latest
    # version in the current branch.
    commit_list = ''
    if lazylibrarian.CONFIG['LATEST_VERSION'] == 'Not_Available_From_GitHUB':
        commits = 0  # don't report a commit diff as we don't know anything
    if lazylibrarian.CONFIG['CURRENT_VERSION'] and commits != 0:
        logger.info('[VersionCheck] -  Comparing currently installed version with latest github version')
        url = 'https://api.github.com/repos/%s/LazyLibrarian/compare/%s...%s' % (
            lazylibrarian.CONFIG['GIT_USER'], lazylibrarian.CONFIG['CURRENT_VERSION'], lazylibrarian.CONFIG['LATEST_VERSION'])
        logger.debug('(getCommitDifferenceFromGit) -  Check for differences between local & repo by [%s]' % url)

        try:
            request = urllib2.Request(url)
            request.add_header('User-Agent', USER_AGENT)
            resp = urllib2.urlopen(request, timeout=30)
            result = resp.read()
            try:
                logger.debug('JSONDecode url')
                git = simplejson.JSONDecoder().decode(result)
                logger.debug('pull total_commits from json object')
                commits = int(git['total_commits'])

                logger.debug('(getCommitDifferenceFromGit) -  GitHub reports as follows Status [%s] - Ahead [%s] - Behind [%s] - Total Commits [%s]' % (
                             git['status'], git['ahead_by'], git['behind_by'], git['total_commits']))

                if int(git['total_commits']) > 0:
                    messages = []
                    for item in git['commits']:
                        messages.insert(0, item['commit']['message'])
                    for line in messages:
                        commit_list = "%s\n%s" % (commit_list, line)
            except Exception:
                logger.warn('(getCommitDifferenceFromGit) -  could not get difference status from GitHub')

        except Exception:
            logger.warn(
                '(getCommitDifferenceFromGit) -  Could not get commits behind from github. Can happen if you have a local commit not pushed to repo or no connection to github')

        if commits > 1:
            logger.info('[VersionCheck] -  New version is available. You are %s commits behind' % commits)
        elif commits == 1:
            logger.info('[VersionCheck] -  New version is available. You are one commit behind')
        elif commits == 0:
            logger.info('[VersionCheck] -  lazylibrarian is up to date ')
        elif commits < 0:
            logger.info(
                '[VersionCheck] -  You are running an unknown version of lazylibrarian. Run the updater to identify your version')

    elif lazylibrarian.CONFIG['LATEST_VERSION'] == 'Not_Available_From_GitHUB':
        commit_list = 'Unable to get latest version from GitHub'
        logger.info(commit_list)
    else:
        logger.info('You are running an unknown version of lazylibrarian. Run the updater to identify your version')

    logger.debug('(getCommitDifferenceFromGit) - exiting with commit value of [%s]' % commits)
    # lazylibrarian.CONFIG['COMMITS_BEHIND'] = commits
    return commits, commit_list


#
# writes a version.txt file in the LL root dir with value of parameter
def updateVersionFile(new_version_id):
        # Update version.txt located in LL home dir.
        version_path = os.path.join(lazylibrarian.PROG_DIR, 'version.txt')

        try:
            logger.debug("(updateVersionFile) Updating [%s] with value [%s]" % (
                version_path, new_version_id))
            if os.path.exists(version_path):
                ver_file = open(version_path, 'r')
                current_version = ver_file.read().strip(' \n\r')
                ver_file.close()
                if current_version == new_version_id:
                    return False

            ver_file = open(version_path, 'w')
            ver_file.write(new_version_id)
            ver_file.close()
            return True
        except IOError as e:
            logger.error(
                u"(updateVersionFile) Unable to write current version to version.txt, update not complete: %s" % str(e))
            return False


def update():
    if lazylibrarian.CONFIG['INSTALL_TYPE'] == 'win':
        logger.debug('(update) Windows install - no update available')
        logger.info('(update) Windows .exe updating not supported yet.')
        return False
    elif lazylibrarian.CONFIG['INSTALL_TYPE'] == 'package':
        logger.debug('(update) Package install - no update available')
        logger.info('(update) Please use your package manager to update')
        return False

    elif lazylibrarian.CONFIG['INSTALL_TYPE'] == 'git':
        branch = getCurrentGitBranch()

        _, _ = runGit('stash clear')
        output, err = runGit('pull origin ' + branch)

        success = True
        if not output:
            logger.error('(update) Couldn\'t download latest version')
            success = False
        for line in output.split('\n'):
            if 'Already up-to-date.' in line:
                logger.info('(update) No update available, not updating')
                logger.info('(update) Output: ' + str(output))
                success = False
            elif line.endswith('Aborting.'):
                logger.error('(update) Unable to update from git: ' + line)
                logger.info('(update) Output: ' + str(output))
                success = False
        if success:
            lazylibrarian.CONFIG['GIT_UPDATED'] = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
            return True
    elif lazylibrarian.CONFIG['INSTALL_TYPE'] == 'source':

        # As this is a non GIT install, we assume that the comparison is
        # always to master.

        tar_download_url = 'https://github.com/%s/%s/tarball/%s' % (
            lazylibrarian.CONFIG['GIT_USER'], lazylibrarian.CONFIG['GIT_REPO'], lazylibrarian.CONFIG['GIT_BRANCH'])
        update_dir = os.path.join(lazylibrarian.PROG_DIR, 'update')
        # version_path = os.path.join(lazylibrarian.PROG_DIR, 'version.txt')

        try:
            logger.info('(update) Downloading update from: ' + tar_download_url)
            request = urllib2.Request(tar_download_url)
            request.add_header('User-Agent', USER_AGENT)
            data = urllib2.urlopen(request, timeout=30)
        except socket.timeout:
            logger.error("(update) Timeout retrieving new version from " + tar_download_url)
            return False
        except (urllib2.HTTPError, urllib2.URLError) as e:
            if hasattr(e, 'reason'):
                errmsg = e.reason
            else:
                errmsg = str(e)
            logger.error(
                "(update) Unable to retrieve new version from " + tar_download_url + ", can't update: %s" % errmsg)
            return False

        download_name = data.geturl().split('/')[-1]

        tar_download_path = os.path.join(lazylibrarian.PROG_DIR, download_name)

        # Save tar to disk
        f = open(tar_download_path, 'wb')
        f.write(data.read())
        f.close()

        # Extract the tar to update folder
        logger.info('(update) Extracting file' + tar_download_path)
        tar = tarfile.open(tar_download_path)
        tar.extractall(update_dir)
        tar.close()

        # Delete the tar.gz
        logger.info('(update) Deleting file' + tar_download_path)
        os.remove(tar_download_path)

        # Find update dir name
        update_dir_contents = [x for x in os.listdir(update_dir) if os.path.isdir(os.path.join(update_dir, x))]
        if len(update_dir_contents) != 1:
            logger.error(u"(update) Invalid update data, update failed: " + str(update_dir_contents))
            return False
        content_dir = os.path.join(update_dir, update_dir_contents[0])

        # walk temp folder and move files to main folder
        for dirname, dirnames, filenames in os.walk(content_dir):
            dirname = dirname[len(content_dir) + 1:]
            for curfile in filenames:
                old_path = os.path.join(content_dir, dirname, curfile)
                new_path = os.path.join(lazylibrarian.PROG_DIR, dirname, curfile)

                if os.path.isfile(new_path):
                    os.remove(new_path)
                os.renames(old_path, new_path)

        # Update version.txt
        updateVersionFile(lazylibrarian.CONFIG['LATEST_VERSION'])
        lazylibrarian.CONFIG['GIT_UPDATED'] = time.ctime()
        return True
    else:
        logger.error("(update) Cannot perform update - Install Type not set")
        return False
