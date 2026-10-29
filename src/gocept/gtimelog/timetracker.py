# encoding=utf-8

import datetime
import requests
from requests.auth import HTTPBasicAuth
import gocept.gtimelog.core
import gocept.gtimelog.bugtracker
import logging
import transaction
import os
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


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


class Project(object):

    def __init__(self, name):
        self.match_string = name


class Timetracker(object):

    def __init__(self, settings):
        self.settings = settings
        self.trackers = gocept.gtimelog.bugtracker.Bugtrackers(self.settings)
        self._mapping = None

    @property
    def PROJECTS(self):
        return self._load()[0]

    @property
    def TASKS(self):
        return self._load()[1]

    def _load(self):
        if self._mapping is None:
            self._mapping = load_mapping(self.settings.timetracker_dsn)
        return self._mapping

    def format_duration(self, duration):
        hours = int(duration.seconds / 3600)
        minutes = int((duration.seconds - hours * 3600) / 60)
        minutes = '{:02d}'.format(int(5 * round(minutes/5.0)))
        if minutes == '60':
            hours += 1
            minutes = '00'
        return '%s:%sh' % (hours, minutes)

    def _get_project(self, project):
        result = None
        if project == 'ClaimX:mci (Produkt)':
            project = 'ClaimXMCI'
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
            import pdb; pdb.set_trace()  # XXXXXXXXXX 
            raise ValueError('No task found for %s: %s:' % (project, task))
        return list(found.values())[0]

    def _get_task_title(self, project, task):
        for k, v in list(self.TASKS[project].items()):
            if v.lower() == task.lower():
                return k

    def report(self, entries):
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

        s = requests.Session()
        pwd = os.environ.get('TIMETRACKER_PASSWORD')
        basic = HTTPBasicAuth('timetracker', pwd)
        s.post(
            'https://timetracker.risclog.de/@@login',
            data={
                'form.login': self.settings.timetracker_username,
                'form.password': self.settings.timetracker_password
            },
            auth=basic
        )

        s.post(
            'https://timetracker.risclog.de/times/@@index',
            data={
                'form.t_date': '2532170f2a682b35bbcaddc088ed1e72',
                'form.t_filter': 'die letzten 31 Tage',
                'form.t_user': '33e75ff09dd601bbe69f351039152189'
            },
            auth=basic
        )

        resp = s.get(
            'https://timetracker.risclog.de/times/@@index',
            auth=basic
        )
        bs = BeautifulSoup(resp.text, features="html.parser")

        existacts = {}
        for act in bs.find_all('table')[-1].find_all('span'):
            ti_id = act.find_all('a')[0].attrs['href'].split('ti_id=')[1]
            data = act.find_all('td')
            project = self._get_project(data[1].text)
            task = self._get_task(project, data[2].text)
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
                        self.set_duplicate(s, act, id_, basic)
            except KeyError:
                self.add_entry(s, act, basic)
            else:
                self.update_entry(s, act, existing_id, basic)

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
            import pdb; pdb.set_trace()  # XXXXXXXXXX 
        project = self._get_project(project.strip())
        return project, self._get_task(project, task.strip()), desc.strip()

    def _get_invoice_flag(self, act):
        if act['task'] in ('6162', '5470'):
            return 'off'
        if act['project'] in ('4018',):
            return 'off'
        return 'on'

    def _get_form_data(self, act, ti_id=''):
        return {
            'form.ti_id': (None, ti_id),
            'form.performed': (None, act['date']),
            'form.project.data': (None, '5420'),
            'form.project.group': (None, act['project']),
            'form.project.item': (None, act['task']),
            'form.effort.hour': (None, act['duration'].split(':')[0]),
            'form.effort.min': (
                None, str(int(act['duration'][:-1].split(':')[1]))
            ),
            'form.description': (None, act['desc']),
            'form.invoice': (None, self._get_invoice_flag(act)),
            'form.invoice.used': (None, ''),
            'form.km': (None, ''),
            'form.price': (None, ''),
            'camefrom': (None, ''),
            'changes': (None, ''),
            'fkt_changes': (None, ''),
            'form.actions.abschicken': (None, 'Abschicken'),
        }

    def _get_form_headers(self):
        return {
            'referer': 'https://timetracker.risclog.de/times/add',
            'origin': 'https://timetracker.risclog.de',
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

    def add_entry(self, s, act, basic):
        resp = s.post(
            'https://timetracker.risclog.de/times/add',
            headers=self._get_form_headers(),
            auth=basic,
            files=self._get_form_data(act)
        )
        resp = BeautifulSoup(resp.text, features="html.parser")

        if resp.title.text == 'System Error':
            self.handle_error(act, resp.title.text)
            return

        errors = resp.find_all('ul')
        if errors:
            self.handle_error(act, errors[0].text)
            return

    def set_duplicate(self, s, act, ti_id, basic):
        act['date'] = '31.01.2021'
        act['project'] = '4018'
        act['task'] = '5641'
        act['duration'] = '0:05h'
        act['desc'] = 'fehlerhafter Eintrag, bitte löschen'
        resp = s.post(
            'https://timetracker.risclog.de/times/edit',
            headers=self._get_form_headers(),
            auth=basic,
            files=self._get_form_data(act, ti_id)
        )
        resp = BeautifulSoup(resp.text, features="html.parser")

        if resp.title.text == 'System Error':
            self.handle_error(act, resp.title.text, ti_id)
            return

        errors = resp.find_all('ul')
        if errors:
            self.handle_error(act, errors[0].text, ti_id)
            return

    def update_entry(self, s, act, ti_id, basic):
        resp = s.post(
            'https://timetracker.risclog.de/times/edit',
            headers=self._get_form_headers(),
            auth=basic,
            files=self._get_form_data(act, ti_id)
        )
        resp = BeautifulSoup(resp.text, features="html.parser")

        if resp.title.text == 'System Error':
            self.handle_error(act, resp.title.text, ti_id)
            return

        errors = resp.find_all('ul')
        if errors:
            self.handle_error(act, errors[0].text, ti_id)
            return
