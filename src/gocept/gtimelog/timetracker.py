# encoding=utf-8

import datetime
import math
import requests
from requests.auth import HTTPBasicAuth
import gocept.gtimelog.core
import gocept.gtimelog.bugtracker
import logging
import transaction
import os
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

SUCCESS_MESSAGE = 'Die Zeit wurde erfolgreich erfasst.'


def load_mapping(dsn):
    """Load projects and subprojects from the ttdata postgres database.

    Returns (PROJECTS, TASKS): {project name: p_id} and
    {p_id: {subproject name: s_id}}, ids as strings.
    """
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute('SELECT name, p_id FROM projects')
        projects = {name: str(p_id) for name, p_id in cur.fetchall()}
        cur.execute('SELECT pid, name, s_id FROM subprojects')
        tasks = {}
        for pid, name, s_id in cur.fetchall():
            tasks.setdefault(str(pid), {})[name] = str(s_id)
    finally:
        conn.close()
    return projects, tasks


def load_mapping_from_form(doc):
    """Read projects and subprojects from the 'times/add' form.

    Returns the same structure as `load_mapping`.
    """
    projects = {}
    tasks = {}
    select = doc.find('select', attrs={'name': 'form.project.data'})
    for group in (select.find_all('optgroup') if select else []):
        p_id = (group.get('title') or '').strip()
        if not p_id:
            continue
        projects[(group.get('label') or p_id).strip()] = p_id
        tasks[p_id] = {
            option.text.strip(): option['value']
            for option in group.find_all('option') if option.get('value')}
    if not projects:
        raise RuntimeError('Timetracker did not return a project form.')
    return projects, tasks


class Project(object):

    def __init__(self, name):
        self.match_string = name


class Timetracker(object):

    def __init__(self, settings):
        self.settings = settings
        self.trackers = gocept.gtimelog.bugtracker.Bugtrackers(self.settings)
        self._mapping = None
        self._connection = None
        self._invoice_cache = {}

    @property
    def PROJECTS(self):
        return self._load()[0]

    @property
    def TASKS(self):
        return self._load()[1]

    def _load(self):
        if self._mapping is None:
            if self.settings.timetracker_dsn:
                self._mapping = load_mapping(self.settings.timetracker_dsn)
            else:
                s, basic = self._connect()
                resp = s.get(self._url('/times/add'), auth=basic)
                resp.raise_for_status()
                self._mapping = load_mapping_from_form(
                    BeautifulSoup(resp.text, features='html.parser'))
        return self._mapping

    def _url(self, path):
        return self.settings.timetracker_url.rstrip('/') + path

    def _connect(self):
        """Log in once and return (session, basic auth)."""
        if self._connection is not None:
            return self._connection
        pwd = (self.settings.timetracker_basic_password or
               os.environ.get('TIMETRACKER_PASSWORD'))
        if not pwd:
            raise RuntimeError(
                'Set basic_password in the [timetracker] section of '
                'gtimelogrc or the TIMETRACKER_PASSWORD environment '
                'variable.')
        basic = HTTPBasicAuth(self.settings.timetracker_basic_username, pwd)
        s = requests.Session()
        resp = s.post(
            self._url('/@@login'),
            data={
                'form.login': self.settings.timetracker_username,
                'form.password': self.settings.timetracker_password,
                'form.camefrom': '/tasks/add',
                'form.actions.login': 'login',
            },
            auth=basic)
        resp.raise_for_status()
        if self._has_login_form(BeautifulSoup(
                resp.text, features='html.parser')):
            raise RuntimeError(
                'Timetracker login failed, check username/password.')
        self._connection = s, basic
        return self._connection

    @staticmethod
    def _has_login_form(doc):
        return bool(doc.find('input', attrs={'type': 'password'}) or
                    doc.find(attrs={'name': 'form.password'}))

    def format_duration(self, duration):
        """Format as H:MMh, always rounding up to the next 5 minutes."""
        minutes = int(math.ceil(duration.total_seconds() / 300.0 - 1e-9)) * 5
        return '%s:%02dh' % (minutes // 60, minutes % 60)

    def _get_project(self, project):
        result = None
        if project == 'ClaimXMCI':
            # the real name contains a colon, which is the timelog separator
            project = 'ClaimX:mci (Produkt)'
        for k in self.PROJECTS:
            if k.lower() == project.lower():
                return self.PROJECTS[k]
        for k in list(self.PROJECTS.keys()):
            if k.lower().startswith(project.lower()):
                if result is not None:
                    raise ValueError(
                        'Found multiple projects for %s: %s, %s' % (
                            project, result, k
                        )
                    )
                result = k
        if result is None:
            raise KeyError('No project found for %s' % project)
        return self.PROJECTS[result]

    def _get_project_title(self, project):
        for k, v in list(self.PROJECTS.items()):
            if v.lower() == project.lower():
                return k

    def _find_tasks(self, project, task):
        """Return {title: id}; prefix matches win over substring matches."""
        task = task.strip().lower()
        tasks = self.TASKS.get(project, {})
        for match in (str.__eq__, str.startswith, str.__contains__):
            found = {k: v for k, v in tasks.items()
                     if match(k.lower(), task)}
            if found:
                return found
        return {}

    def _get_task(self, project, task):
        found = self._find_tasks(project, task)
        if len(found) > 1:
            raise ValueError(
                'Found multiple tasks for %s: %s' % (
                    task, ', '.join(sorted(found))))
        if not found:
            raise ValueError('No task found for %s: %s:' % (project, task))
        return list(found.values())[0]

    def _get_task_title(self, project, task):
        for k, v in list(self.TASKS[project].items()):
            if v.lower() == task.lower():
                return k

    def report(self, entries, dirty=False):
        """Upload entries; with dirty=True only print what would be done."""
        subjects = {}
        activities = []
        for start, stop, duration, entry in entries:
            if not duration:
                continue
            if entry.endswith('**'):
                continue
            if entry.endswith('$$$'):  # we don't track holidays
                continue
            if start > stop:
                raise ValueError("End before begin in %s (%s to %s)" % (
                                 entry, start, stop))
            project, task, desc = self.mapEntry(entry)

            break_ = datetime.timedelta(0)
            if desc.endswith('/2'):
                # if task ends with /2 divide the duration by two
                desc = desc[:-2]
                break_ = duration / 2
            log.debug("%s -> %s, [%s] [%s] %s %s" % (
                start, stop, project, task, duration, desc))

            duration = duration - break_

            updated = False
            for activity in activities:
                if (
                    activity['project'] == project and
                    activity['task'] == task and
                    activity['date'] == start.strftime('%d.%m.%Y') and
                    activity['desc'] == desc
                ):
                    updated = True
                    activity['orig_duration'] += duration
                    activity['duration'] = self.format_duration(
                        activity['orig_duration']
                    )

            if not updated:
                activities.append(dict(
                    date=start.strftime('%d.%m.%Y'),
                    project=project,
                    task=task,
                    desc=desc,
                    duration=self.format_duration(duration),
                    orig_duration=duration,
                    ))

        s, basic = self._connect()

        s.post(
            self._url('/times/@@index'),
            files={
                'form.t_date': (None, self.settings.timetracker_filter_date),
                'form.t_date-empty-marker': (None, '1'),
                'form.t_filter': (None, 'die letzten 31 Tage'),
                'form.t_filter-empty-marker': (None, '1'),
                'form.t_user': (None, self.settings.timetracker_filter_user),
                'form.t_user-empty-marker': (None, '1'),
                # Without the submit button the filter is not applied.
                'form.actions.46696c74657220616e77656e64656e': (
                    None, 'Filter anwenden'),
            },
            auth=basic
        )

        resp = s.get(self._url('/times/@@index'), auth=basic)
        resp.raise_for_status()
        bs = BeautifulSoup(resp.text, features="html.parser")

        existacts = {}
        tables = bs.find_all('table')
        if not tables and bs.find('input', attrs={'type': 'password'}):
            raise RuntimeError(
                'Timetracker login failed, check username/password.')
        # No table means there are no existing entries in the filter range.
        spans = tables[-1].find_all('span') if tables else []
        for act in spans:
            ti_id = act.find_all('a')[0].attrs['href'].split('ti_id=')[1]
            data = act.find_all('td')
            try:
                project = self._get_project(data[1].text)
                task = self._get_task(project, data[2].text)
            except (KeyError, ValueError):
                # E.g. closed tasks are missing in the form; entries we
                # cannot map can never match an entry we upload.
                log.debug('Ignoring existing entry %s: %s / %s', ti_id,
                          data[1].text, data[2].text)
                continue
            duration = data[3].text
            date = data[6].text
            desc = data[11].text.strip()
            if date not in existacts:
                existacts[date] = {}
            if project not in existacts[date]:
                existacts[date][project] = {}
            if task not in existacts[date][project]:
                existacts[date][project][task] = []
            existacts[date][project][task].append(dict(
                duration=duration, desc=desc, id=ti_id))

        for act in activities:
            try:
                tempacts = existacts[act['date']][act['project']][act['task']]
                if act['desc'] not in [d['desc'] for d in tempacts]:
                    raise KeyError(act['desc'])
                existing_ids = [
                    d['id'] for d in tempacts if d['desc'] == act['desc']
                ]
                if len(existing_ids) == 1:
                    existing_id = existing_ids[0]
                elif len(existing_ids) == 0:
                    raise RuntimeError(
                        'No existing ID found for existing task'
                    )
                else:
                    existing_id = existing_ids[0]
                    for id_ in existing_ids[1:]:
                        if dirty:
                            print(self._describe(
                                'DUPLICATE', act, id_, s, basic))
                        else:
                            self.set_duplicate(s, act, id_, basic)
            except KeyError:
                if dirty:
                    print(self._describe('NEW', act, None, s, basic))
                else:
                    self.add_entry(s, act, basic)
            else:
                if dirty:
                    print(self._describe(
                        'UPDATE', act, existing_id, s, basic))
                else:
                    self.update_entry(s, act, existing_id, basic)

    def _describe(self, action, act, ti_id=None, s=None, basic=None):
        project_title = self._get_project_title(act['project'])
        task_title = self._get_task_title(act['project'], act['task'])
        invoice = 'ja' if self._get_invoice(s, act, basic) else 'nein'
        return '%-9s %s %s (%s) / %s (%s) %s  %s%s [fakturiert: %s]' % (
            action, act['date'], project_title, act['project'],
            task_title, act['task'], act['duration'], act['desc'],
            ' [ti_id=%s]' % ti_id if ti_id else '', invoice)

    def annotate_entry(self, entry):
        """Append the project and task ids in parentheses to an entry.

        Returns the entry unchanged if it cannot be mapped.
        """
        parts = entry.split(':')
        if len(parts) != 3:
            return entry
        project, task, desc = parts
        try:
            project_id = self._get_project(project.strip())
            matches = list(self._find_tasks(project_id, task).values())
        except Exception:
            return entry
        if len(matches) != 1:
            return entry
        return '%s (%s):%s (%s):%s' % (
            project.strip(), project_id, task.rstrip(), matches[0], desc)

    def mapEntry(self, entry):
        try:
            project, task, desc = entry.split(':')
        except ValueError:
            raise ValueError(
                "Entry needs the form 'project: task: description': %s" %
                entry)
        project = self._get_project(project.strip())
        return project, self._get_task(project, task.strip()), desc.strip()

    def _get_invoice(self, s, act, basic):
        """Ask the timetracker whether the entry is invoiced by default."""
        key = (act['project'], act['task'])
        if key not in self._invoice_cache:
            resp = s.get(
                self._url('/times/set_invoice'),
                params={'project': act['project'], 'subproject': act['task']},
                # The server answers with a 500 without a referer.
                headers=self._get_form_headers(),
                auth=basic)
            resp.raise_for_status()
            value = resp.text.strip()
            if value not in ('true', 'false'):
                raise RuntimeError(
                    'Timetracker returned an invalid invoicing setting '
                    '%r for %s/%s.' % (value, act['project'], act['task']))
            self._invoice_cache[key] = value == 'true'
        return self._invoice_cache[key]

    def _get_form_data(self, act, ti_id='', invoice=False):
        data = {
            'form.ti_id': (None, ti_id),
            'form.performed': (None, act['date']),
            'form.project.group': (None, act['project']),
            'form.project.item': (None, act['task']),
            'form.effort.hour': (None, act['duration'].split(':')[0]),
            'form.effort.min': (
                None, str(int(act['duration'][:-1].split(':')[1]))
            ),
            'form.description': (None, act['desc'].strip()),
            'form.invoice.used': (None, ''),
            'form.km': (None, ''),
            'form.price': (None, ''),
            'camefrom': (None, ''),
            'changes': (None, ''),
            'fkt_changes': (None, ''),
            'form.actions.abschicken': (None, 'Abschicken'),
        }
        if invoice:
            # An unchecked checkbox is not submitted at all.
            data['form.invoice'] = (None, 'on')
        return data

    def _get_form_headers(self):
        return {
            'referer': self._url('/times/add'),
            'origin': self.settings.timetracker_url.rstrip('/'),
            'user-agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_5) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/84.0.4147.89 Safari/537.36'
            ),
            'accept': (
                'text/html,application/xhtml+xml,application/xml;q=0.9,'
                'image/webp,image/apng,*/*;q=0.8,application/signed-'
                'exchange;v=b3;q=0.9'
            ),
            'accept-language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
        }

    def handle_error(self, act, msg, ti_id=''):
        msg = msg.strip()
        msg = "%s %s | %s: %s: %s -> %s (%s)" % (
                act['date'], act['duration'],
                self._get_project_title(act['project']),
                self._get_task_title(act['project'], act['task']),
                act['desc'], msg, ti_id)
        print(msg)

    def _validate(self, act):
        """Return an error message if the entry cannot be uploaded."""
        if not act['project'] or not act['task']:
            return 'Project and task are required.'
        if not act['desc'].strip():
            return 'Description is empty.'
        if act['duration'] in ('0:00h', ''):
            return 'Duration must be positive.'
        return None

    def _post(self, s, act, basic, path, ti_id=''):
        """POST the entry and report problems. Returns True on success."""
        error = self._validate(act)
        if error:
            self.handle_error(act, error, ti_id)
            return False
        resp = s.post(
            self._url(path),
            headers=self._get_form_headers(),
            auth=basic,
            files=self._get_form_data(
                act, ti_id, self._get_invoice(s, act, basic))
        )
        if not resp.ok:
            self.handle_error(act, 'HTTP %s' % resp.status_code, ti_id)
            return False
        doc = BeautifulSoup(resp.text, features="html.parser")
        if self._has_login_form(doc):
            raise RuntimeError('Timetracker session expired.')
        if doc.title and doc.title.text.strip() == 'System Error':
            self.handle_error(act, 'System Error', ti_id)
            return False
        message = doc.find('ul')
        message = message.text.strip() if message else ''
        if message == SUCCESS_MESSAGE:
            return True
        if message:
            self.handle_error(act, message, ti_id)
            return False
        if not ti_id and not (
                resp.history and
                resp.url.rstrip('/').endswith('/times') and
                not doc.find(attrs={'name': 'form.performed'})):
            self.handle_error(
                act, 'Submission outcome is uncertain, check the '
                'timetracker before retrying.')
            return False
        return True

    def add_entry(self, s, act, basic):
        return self._post(s, act, basic, '/times/add')

    def set_duplicate(self, s, act, ti_id, basic):
        act['date'] = '31.01.2021'
        act['project'] = '4018'
        act['task'] = '5641'
        act['duration'] = '0:05h'
        act['desc'] = 'fehlerhafter Eintrag, bitte löschen'
        return self._post(s, act, basic, '/times/edit', ti_id)

    def update_entry(self, s, act, ti_id, basic):
        return self._post(s, act, basic, '/times/edit', ti_id)
