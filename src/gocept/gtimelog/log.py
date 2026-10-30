# Copyright (c) 2013 gocept gmbh & co. kg
# See also LICENSE.txt

import argparse
import gocept.gtimelog.cli
import gocept.gtimelog.redmine
import gocept.gtimelog.timetracker
import os
import os.path
import re


def log():
    """Create a new logentry"""
    # Start logging
    # Argument parsing
    parser = argparse.ArgumentParser(
        description=u'Add a new timelog entry.')
    parser.add_argument(
        'log',
        nargs='*',
        help='Your log.')
    args = parser.parse_args()

    # Load config
    settings, timelog = gocept.gtimelog.cli.load_config_and_timelog()
    text = ' '.join(args.log).strip()
    if re.match(r'^\d+$', text):
        text = entry_from_ticket(settings, int(text))
        print(text)
    timelog.append(text)


TT_FIELD_NAME = 'projekt tt'
TT_IDS = re.compile(r'\((\d+)\|(\d+)\)')


def entry_from_ticket(settings, issue_id):
    """Build a timelog entry 'Project: Subproject: #id Title' for a ticket."""
    if not settings.redmines:
        raise SystemExit('No [redmine] section configured in gtimelogrc.')
    config = settings.redmines[0]
    redmine = gocept.gtimelog.redmine.Redmine(
        config['url'], config['api_key'], config['activity'],
        config['projects'])
    try:
        issue = redmine.api('Issue').find(
            issue_id, include='custom_fields')
    except Exception as exc:
        raise SystemExit('Could not read ticket #%s: %s' % (issue_id, exc))

    value = None
    for field in getattr(issue, 'custom_fields', None) or []:
        if (getattr(field, 'name', '') or '').strip().lower() == TT_FIELD_NAME:
            value = getattr(field, 'value', None)
    match = value and TT_IDS.search(str(value))
    if not match:
        raise SystemExit(
            'Ticket #%s has no usable "Projekt TT" value (%r).' % (
                issue_id, value))
    project_id, task_id = match.groups()

    timetracker = gocept.gtimelog.timetracker.Timetracker(settings)
    project = timetracker._get_project_title(project_id)
    task = project and timetracker._get_task_title(project_id, task_id)
    if not project or not task:
        raise SystemExit(
            'Projekt TT (%s|%s) of ticket #%s not found in the database.' % (
                project_id, task_id, issue_id))

    # ':' separates the timelog fields, '#' marks the ticket number.
    def clean(text):
        return text.replace(':', ' -').replace('#', '').strip()

    if ':' in project:
        project = 'ClaimXMCI' if project == 'ClaimX:mci (Produkt)' \
            else clean(project)
    return '%s: %s: #%s %s' % (
        project, clean(task), issue_id, clean(issue.subject))


def download():
    """Download projects and tasks."""

    settings, timelog = gocept.gtimelog.cli.load_config_and_timelog()
    configdir = os.path.expanduser('~/.gtimelog')
    tasks = gocept.gtimelog.collmex.TaskList(
        os.path.join(configdir, settings.collmex_task_file), settings)
    tasks.reload()


def open_editor():
    os.system("open -a MacVim.app ~/.gtimelog/timelog.txt")
