"""A synthetic downloads folder (about 200 files) with the traps met on a real one. No real data."""
import datetime
import os
import time
import unicodedata
import zipfile

OLD, NEW = datetime.datetime(2023, 3, 10, 12), datetime.datetime(2025, 6, 2, 12)  # either side of the 2024-05-15 entity switch


def tiny_pdf(text):
    """A valid one-page PDF whose text pdftotext can read."""
    stream = f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET'.encode()
    objs = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
            b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
            b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream',
            b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    out, offsets = b'%PDF-1.4\n', []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % i + o + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1) + b''.join(b'%010d 00000 n \n' % o for o in offsets)
    return out + b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objs) + 1, xref)


def tiny_docx(text):
    def write(p):
        with zipfile.ZipFile(p, 'w') as z:
            z.writestr('word/document.xml', f'<w:document><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return write


def make_tree(root):
    """Writes the fixtures under root and returns the number of files."""
    files = {
        'INV-2025-001-Acme.pdf': (tiny_pdf('Invoice from Acme'), NEW),
        'inv-2025-002-supplier.pdf': (tiny_pdf('Invoice'), NEW),           # a supplier with our numbering
        'scan_0001.pdf': (tiny_pdf('Invoice amount due 120 GBP'), NEW),
        'document.pdf': (tiny_pdf('This agreement is made between the parties'), NEW),
        'file_0042.docx': (tiny_docx('Payslip pay period March'), OLD),
        'calendar.ics': (b'BEGIN:VCALENDAR', NEW),                          # not an NDA
        'confirmation.pdf': (tiny_pdf('Thank you'), NEW),
        'kitchen.jpg': (b'\xff\xd8 kitchen', NEW),
        'Globex NDA.pdf': (tiny_pdf('Mutual NDA'), NEW),
        'Globex invoice 7.pdf': (tiny_pdf('Invoice'), NEW),                 # admin theme: not routed to the client
        'old studio deck.pptx': (b'pptx', OLD),
        'pitch.pptx': (b'pitch', NEW),
        'report.pdf': (tiny_pdf('Annual report'), NEW),
        'report (1).pdf': (tiny_pdf('Annual report'), NEW),                 # exact duplicate
        'IMG_2041.HEIC': (b'heic', OLD),
        'Screenshot 2025-06-01 at 10.00.00.png': (b'png', NEW),
        'export_2025-02-14.csv': (b'date,sales\n2025-02-14,3\n', NEW),
        'passport scan.pdf': (tiny_pdf('Passport'), OLD),
        'tender for regional council.pdf': (tiny_pdf('Invitation to tender'), NEW),
        'paper.txt': (b'notes', NEW),
        'x' * 240 + '.pdf': (tiny_pdf('Long name'), NEW),                    # 244 bytes: a '.part' suffix would break the limit
        unicodedata.normalize('NFD', 'Relevé de compte bank statement.pdf'): (tiny_pdf('Statement'), NEW),
        'PDF/lease renewal.pdf': (tiny_pdf('Lease'), OLD),                  # inside a browser type folder
        'Board\xa0pack Q1/minutes.pdf': (tiny_pdf('Minutes'), NEW),          # known folder, non-breaking space in the name
        'Board\xa0pack Q1/agenda.pdf': (tiny_pdf('Agenda'), NEW),
        'Random project/notes.md': (b'# notes', NEW),                       # unknown folder, moved whole
    }
    words = ['invoice', 'receipt', 'payslip', 'agreement', 'deck', 'statement', 'whitepaper', 'booking confirmation', 'unknown thing', 'quote']
    for i in range(170):                                                    # bulk, with varied names and dates
        w = words[i % len(words)]
        files[f'{w} {i:03d}.pdf'] = (tiny_pdf(f'{w} number {i}'), OLD if i % 3 == 0 else NEW)
    for rel, (content, when) in files.items():
        p = os.path.join(root, *rel.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if callable(content):
            content(p)
        else:
            with open(p, 'wb') as f:
                f.write(content)
        t = time.mktime(when.timetuple())
        os.utime(p, (t, t))
    return len(files)
