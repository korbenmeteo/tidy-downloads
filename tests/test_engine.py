import datetime
import unicodedata
import unittest
from pathlib import Path

from tidy_downloads.engine import Rules, classify, file_date, folder_key, hit, name_date, norm

EXAMPLE = Path(__file__).parents[1] / 'src' / 'tidy_downloads' / 'rules.example.toml'
D = datetime.date


class NameDate(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(name_date('report 2025-03-14.pdf'), D(2025, 3, 14))
        self.assertEqual(name_date('report_20250314.pdf'), D(2025, 3, 14))
        self.assertEqual(name_date('14-03-2025 minutes'), D(2025, 3, 14))
        self.assertEqual(name_date('export 2025-03'), D(2025, 3, 1))
        self.assertEqual(name_date('2503-f-2025-001-client'), D(2025, 3, 1))
        self.assertIsNone(name_date('photo 123456'))

    def test_implausible_name_date_ignored(self):
        self.assertEqual(file_date('plan 2029-01-01.pdf', D(2025, 1, 1)), D(2025, 1, 1))  # after mtime + 60 days
        self.assertEqual(file_date('plan 2024-12-20.pdf', D(2025, 1, 1)), D(2024, 12, 20))


class Matching(unittest.TestCase):
    def test_short_words_need_a_boundary(self):
        self.assertIsNone(hit('team calendar', ['nda']))
        self.assertIsNone(hit('order confirmation', ['irm']))
        self.assertIsNone(hit('kitchen photo', ['itc']))
        self.assertEqual(hit('signed nda.pdf', ['nda']), 'nda')
        self.assertEqual(hit('iban3718.pdf', ['iban']), 'iban')  # a digit is a boundary

    def test_long_words_match_inside(self):
        self.assertEqual(hit('mutualconfidentiality.pdf', ['confidential']), 'confidential')

    def test_unicode(self):
        nfd = unicodedata.normalize('NFD', 'Relevé')
        self.assertEqual(norm(nfd), norm('Relevé'))
        self.assertEqual(norm('Relevé'), 'releve')
        self.assertEqual(norm('a\xa0b'), 'a b')
        self.assertEqual(folder_key(unicodedata.normalize('NFD', 'Café\xa0pack ')), 'Café pack')


class Cascade(unittest.TestCase):
    R = Rules.load(EXAMPLE)

    def go(self, rel, d=D(2025, 6, 1), text=''):
        return classify(self.R, rel, d, text)

    def test_issued_invoice_needs_our_name(self):
        self.assertEqual(self.go('INV-2025-001-Acme.pdf').folder, 'Acme/Admin/Invoices/Issued')
        d = self.go('inv-2025-002-supplier.pdf')
        self.assertEqual(d.folder, 'Acme/Admin/Invoices/Received')
        self.assertIn('no entity in the name', d.why)

    def test_entity_by_date_then_by_name_or_id(self):
        self.assertTrue(self.go('payslip.pdf', D(2023, 1, 1)).folder.startswith('Old Studio/'))
        self.assertTrue(self.go('payslip.pdf', D(2025, 1, 1)).folder.startswith('Acme/'))
        self.assertTrue(self.go('payslip.pdf', D(2023, 1, 1), 'company no 123456789').folder.startswith('Acme/'))
        self.assertTrue(self.go('old studio payslip.pdf', D(2023, 1, 1)).folder.startswith('Old Studio/'))
        self.assertFalse(self.go('old studio payslip.pdf', D(2025, 1, 1)).folder.startswith('Old Studio/'))  # name ignored after its end

    def test_entity_specific_folder(self):
        self.assertEqual(self.go('old studio deck.pptx', D(2023, 1, 1)).folder, 'Old Studio/Sales material')
        self.assertEqual(self.go('deck.pptx').folder, 'Acme/Sales/Decks')

    def test_order_personal_media_rules_themes_routes(self):
        self.assertEqual(self.go('passport IMG_1234.jpg').folder, 'Personal/ID & admin')       # personal before media
        self.assertEqual(self.go('IMG_1234.jpg').folder, 'Personal/Photos/2025')
        self.assertEqual(self.go('tender invoice.pdf').folder, 'Acme/Clients/Tenders')           # pro rule before theme
        self.assertEqual(self.go('Globex NDA.pdf').folder, 'Acme/Clients/Globex')                # route for a non-admin theme
        self.assertEqual(self.go('Globex invoice.pdf').folder, 'Acme/Admin/Invoices/Received')   # admin theme: no route
        self.assertEqual(self.go('team calendar.ics').folder, 'Archive/2025/Unidentified')

    def test_personal_unless(self):
        self.assertEqual(self.go('lab results.pdf').folder, 'Personal/Health')
        self.assertNotEqual(self.go('acme lab results.pdf').folder, 'Personal/Health')

    def test_text_only_for_pdf_and_word(self):
        self.assertEqual(self.go('scan.pdf', text='Invoice amount due').folder, 'Acme/Admin/Invoices/Received')
        self.assertNotEqual(self.go('pipeline.xlsx', text='Invoice amount due').folder, 'Acme/Admin/Invoices/Received')

    def test_entity_name_removed_before_theme(self):
        R = Rules({'entities': [{'folder': 'Apple Ltd', 'aliases': ['apple ltd']}],
                   'themes': [{'id': 'fruit', 'dest': 'Fruit', 'keywords': ['apple']},
                              {'id': 'inv', 'dest': 'Invoices', 'keywords': ['invoice']}]})
        self.assertEqual(classify(R, 'Apple Ltd invoice.pdf', D(2025, 1, 1)).folder, 'Apple Ltd/Invoices')

    def test_explain_says_why(self):
        d = self.go('bank statement march.pdf')
        self.assertEqual(d.rule, 'bank')
        self.assertIn("'bank statement'", d.why)
        self.assertIn('entity by date', d.why)


if __name__ == '__main__':
    unittest.main()
