import base64
import csv
import io
import json
import os
import unittest
import zipfile

from fastapi import HTTPException
from piper_pdf_router import GeneralFileRequest, MIME_TYPES, _make_general_file, _safe_general_name, generate_general_file


class GeneralFileGeneratorTests(unittest.TestCase):
    def setUp(self):
        os.environ['PIPER_API_KEY'] = 'unit-test-secret'

    def test_office_and_archive_formats_generate_real_files(self):
        table = json.dumps({'headers': ['Name', 'Score'], 'rows': [['Ada', 95]]})
        slides = json.dumps({'slides': [{'title': 'Introduction', 'bullets': ['Point one', 'Point two']}]})
        cases = {
            'pdf': ('notes.pdf', 'Readable PDF body'),
            'docx': ('notes.docx', '# Heading\nBody paragraph'),
            'xlsx': ('marks.xlsx', table),
            'pptx': ('slides.pptx', slides),
            'csv': ('marks.csv', table),
            'txt': ('notes.txt', 'Plain text'),
            'md': ('notes.md', '# Markdown'),
            'html': ('page.html', '<h1>Hello</h1>'),
            'json': ('data.json', '{"ok":true}'),
            'xml': ('data.xml', '<root><ok>true</ok></root>'),
            'yaml': ('data.yaml', 'ok: true'),
            'css': ('style.css', 'body { margin: 0; }'),
            'js': ('app.js', 'console.log("ok");'),
            'py': ('app.py', 'print("ok")'),
            'php': ('app.php', '<?php echo "ok";'),
            'java': ('App.java', 'class App {}'),
            'cpp': ('main.cpp', 'int main() { return 0; }'),
            'sql': ('query.sql', 'SELECT 1;'),
            'svg': ('icon.svg', '<svg xmlns="http://www.w3.org/2000/svg"></svg>'),
            'zip': ('bundle.zip', ''),
            'rtf': ('notes.rtf', 'Rich text content'),
            'ts': ('app.ts', 'const value: string = \"ok\";'),
            'jsonl': ('records.jsonl', '{\"id\":1}\n{\"id\":2}'),
        }
        for ext, (filename, content) in cases.items():
            payload = GeneralFileRequest(filename=filename, content=content, files=[{'path': 'index.html', 'content': '<h1>Hi</h1>'}] if ext == 'zip' else None, title='Test file')
            output = _make_general_file(payload, ext)
            self.assertTrue(output, ext)
            if ext in {'docx', 'xlsx', 'pptx', 'zip'}:
                self.assertTrue(zipfile.is_zipfile(io.BytesIO(output)), ext)
            if ext == 'pdf':
                self.assertTrue(output.startswith(b'%PDF-'))

    def test_every_advertised_extension_has_a_mime_type(self):
        expected = 'pdf docx xlsx pptx csv txt md html json xml yaml yml css js py php java cpp sql svg zip rtf ts tsx jsx c h cs go rs rb sh ps1 bat toml ini conf properties tex mdx jsonl'.split()
        self.assertEqual(set(expected) - set(MIME_TYPES), set())

    def test_csv_and_json_are_validated(self):
        payload = GeneralFileRequest(filename='table.csv', content=json.dumps({'headers': ['Name', 'Score'], 'rows': [['Ada', 95]]}))
        output = _make_general_file(payload, 'csv').decode('utf-8-sig')
        self.assertEqual(list(csv.reader(io.StringIO(output))), [['Name', 'Score'], ['Ada', '95']])
        with self.assertRaises(HTTPException):
            _make_general_file(GeneralFileRequest(filename='bad.json', content='{broken'), 'json')

    def test_html_is_sanitised_and_zip_paths_are_safe(self):
        payload = GeneralFileRequest(filename='page.html', content='<h1>Hi</h1><script>alert(1)</script><a href="javascript:alert(1)">bad</a>')
        output = _make_general_file(payload, 'html').lower()
        self.assertNotIn(b'<script', output)
        self.assertNotIn(b'javascript:', output)
        unsafe = GeneralFileRequest(filename='bundle.zip', files=[{'path': '../secret.txt', 'content': 'no'}])
        with self.assertRaises(HTTPException):
            _make_general_file(unsafe, 'zip')

    def test_endpoint_auth_filename_and_response_contract(self):
        payload = GeneralFileRequest(filename='../hello.txt', content='Hello')
        response = generate_general_file(payload, 'unit-test-secret')
        self.assertTrue(response['success'])
        self.assertEqual(response['filename'], 'hello.txt')
        self.assertEqual(base64.b64decode(response['fileBase64']), b'Hello')
        self.assertEqual(response['mimeType'], MIME_TYPES['txt'])
        with self.assertRaises(HTTPException):
            generate_general_file(payload, 'wrong-key')
        self.assertEqual(_safe_general_name('../../Quarterly Report.xlsx'), 'Quarterly_Report.xlsx')


if __name__ == '__main__':
    unittest.main()
