# encoding=utf-8
from datetime import timedelta
from gocept.gtimelog.core import Settings
from gocept.gtimelog.timetracker import Timetracker
from gocept.gtimelog.timetracker import load_mapping_from_form
from bs4 import BeautifulSoup
import unittest


class TimetrackerTests(unittest.TestCase):

    def setUp(self):
        self.tt = Timetracker(Settings())

    def test_format_duration_rounds_up_to_five_minutes(self):
        fmt = self.tt.format_duration
        self.assertEqual('0:05h', fmt(timedelta(seconds=1)))
        self.assertEqual('0:05h', fmt(timedelta(minutes=5)))
        self.assertEqual('0:10h', fmt(timedelta(minutes=5, seconds=1)))
        self.assertEqual('1:00h', fmt(timedelta(minutes=56)))
        self.assertEqual('2:30h', fmt(timedelta(hours=2, minutes=26)))

    def test_invoice_checkbox_only_sent_when_enabled(self):
        act = dict(date='01.01.2024', project='1', task='2',
                   duration='1:05h', desc=' foo ')
        self.assertNotIn('form.invoice', self.tt._get_form_data(act))
        data = self.tt._get_form_data(act, invoice=True)
        self.assertEqual((None, 'on'), data['form.invoice'])
        self.assertEqual((None, 'foo'), data['form.description'])
        self.assertEqual((None, '5'), data['form.effort.min'])

    def test_load_mapping_from_form(self):
        doc = BeautifulSoup(
            '<select name="form.project.data">'
            '<optgroup title="10" label="Proj"><option value="">-</option>'
            '<option value="20">Task</option></optgroup></select>',
            features='html.parser')
        self.assertEqual(
            ({'Proj': '10'}, {'10': {'Task': '20'}}),
            load_mapping_from_form(doc))
