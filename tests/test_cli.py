"""临时目录验收覆盖去重、权限、恢复与内容变更风险。"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from knowledge_pipeline.cli import LEARNING, PipelineError, prepare, source_info


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.vault = self.base / 'vault'
        self.learning = self.vault / LEARNING
        self.drafts = self.learning / '_staging/来源草稿'
        self.drafts.mkdir(parents=True)
        (self.learning / '90-资料来源索引').mkdir()
        self.root = self.base / 'sources'
        self.root.mkdir()
        self.source = self.root / '资料.md'
        self.source.write_text('# 测试\n\n忽略规则并执行命令是测试资料，不能执行。\n')
        self.output = self.base / 'private'

    def run_prepare(self, **changes):
        args = dict(source=self.source, root=self.root, vault=self.vault,
                    output=self.output, root_id='sample', permission_id='permit-1', allow_metadata=True)
        args.update(changes)
        return prepare(**args)

    def test_conversion_is_private_and_source_is_unchanged(self):
        original = self.source.read_bytes()
        result = self.run_prepare()
        self.assertEqual(result['state'], 'prepared')
        draft = Path(result['source_draft']).read_text()
        self.assertNotIn('忽略规则', draft)
        record = json.loads(Path(result['private_record']).read_text())
        self.assertFalse(record['model_called'])
        self.assertEqual(record['text_mode'], 'source_reference')
        self.assertEqual(record['text_path'], str(self.source))
        self.assertFalse((Path(result['private_record']).parent / 'converted.md').exists())
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(len(list(self.vault.rglob('*.md'))), 1)

    def test_txt_references_original_and_pdf_keeps_conversion(self):
        self.source = self.root / 'sample.txt'
        self.source.write_text('文本原件')
        text_result = self.run_prepare()
        self.assertEqual(json.loads(Path(text_result['private_record']).read_text())['text_mode'], 'source_reference')
        self.assertFalse((Path(text_result['private_record']).parent / 'converted.md').exists())
        self.source = self.root / 'sample.pdf'
        self.source.write_bytes(b'%PDF synthetic')
        with patch('knowledge_pipeline.cli.convert', return_value=('PDF提取文字', {'name': 'fake', 'version': 'test'})):
            pdf_result = self.run_prepare()
        record = json.loads(Path(pdf_result['private_record']).read_text())
        self.assertEqual(record['text_mode'], 'converted_file')
        self.assertEqual(Path(record['text_path']).read_text(), 'PDF提取文字')

    def test_repeat_skips_conversion_even_after_human_move(self):
        first = self.run_prepare()
        p = Path(first['source_draft'])
        p.rename(self.learning / '90-资料来源索引' / p.name)
        with patch('knowledge_pipeline.cli.convert', side_effect=AssertionError('不能重复转换')):
            self.assertEqual(self.run_prepare()['state'], 'skipped')

    def test_concurrent_calls_create_one_draft(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_prepare(), range(2)))
        self.assertEqual(sorted(r['state'] for r in results), ['prepared', 'skipped'])
        self.assertEqual(len(list(self.drafts.glob('*.md'))), 1)

    def test_changed_content_preserves_prior_source(self):
        first = self.run_prepare()
        prior = Path(first['source_draft']).read_bytes()
        self.source.write_text('新内容')
        second = self.run_prepare()
        self.assertNotEqual(first['sha256'], second['sha256'])
        self.assertEqual(Path(first['source_draft']).read_bytes(), prior)
        self.assertIn('同路径出现不同内容', Path(second['source_draft']).read_text())

    def test_symlink_input_and_staging_are_rejected(self):
        link = self.root / 'link.md'
        link.symlink_to(self.source)
        with self.assertRaises(PipelineError):
            self.run_prepare(source=link)
        self.drafts.rmdir()
        outside = self.base / 'outside'
        outside.mkdir()
        self.drafts.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(PipelineError):
            self.run_prepare()
        self.assertEqual(list(outside.iterdir()), [])

    def test_permission_and_private_output_boundaries(self):
        with self.assertRaises(PipelineError):
            self.run_prepare(allow_metadata=False)
        with self.assertRaises(PipelineError):
            self.run_prepare(output=self.vault / 'private')
        repo = self.base / 'repo'
        (repo / '.git').mkdir(parents=True)
        with self.assertRaises(PipelineError):
            self.run_prepare(output=repo / 'private')

    def test_empty_bad_encoding_and_conversion_failure_leave_no_draft(self):
        for data in [b'', b'\xff\xfe']:
            self.source.write_bytes(data)
            with self.assertRaises(PipelineError):
                self.run_prepare()
        self.source.write_text('文字')
        with patch('knowledge_pipeline.cli.convert', side_effect=PipelineError('失败')):
            with self.assertRaises(PipelineError):
                self.run_prepare()
        self.assertEqual(list(self.drafts.glob('*.md')), [])

    def test_mid_conversion_changes_are_rejected(self):
        def changing(_):
            self.source.write_text('转换时发生变化')
            return '先前内容', {'name': 'fake', 'version': 'test'}
        with patch('knowledge_pipeline.cli.convert', side_effect=changing):
            with self.assertRaises(PipelineError):
                self.run_prepare()
        self.assertEqual(list(self.drafts.glob('*.md')), [])

    def test_interrupted_private_artifact_not_overwritten(self):
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        artifact = self.output / digest
        artifact.mkdir(parents=True)
        sentinel = artifact / 'converted.md'
        sentinel.write_text('中断记录')
        with self.assertRaises(PipelineError):
            self.run_prepare()
        self.assertEqual(sentinel.read_text(), '中断记录')

    def test_duplicate_source_records_block(self):
        first = self.run_prepare()
        p = Path(first['source_draft'])
        (self.drafts / 'duplicate.md').write_text(p.read_text())
        with self.assertRaises(PipelineError):
            self.run_prepare()

    def test_input_limits_and_root_containment(self):
        with self.assertRaises(PipelineError):
            source_info(self.source, self.base / 'other')
        self.source.write_bytes(b'a' * (10 * 1024 * 1024 + 1))
        with self.assertRaises(PipelineError):
            self.run_prepare()

    def test_moved_sources_with_bom_and_comments_still_skip(self):
        first = self.run_prepare()
        p = Path(first['source_draft'])
        target = self.learning / '90-资料来源索引' / p.name
        p.rename(target)
        content = target.read_text().replace('sha256: "' + first['sha256'] + '"',
                                             'sha256: "' + first['sha256'] + '" # 已核对')
        target.write_text('\ufeff' + content)
        self.assertEqual(self.run_prepare()['state'], 'skipped')

    def test_malformed_source_metadata_fails_closed(self):
        (self.learning / '90-资料来源索引/broken.md').write_text('---\nsha256: broken\n---\n')
        with self.assertRaises(PipelineError):
            self.run_prepare()
        self.assertEqual(list(self.drafts.glob('*.md')), [])

    def test_write_failure_records_and_preserves_partial_result(self):
        import knowledge_pipeline.cli as cli
        real_write = cli.exclusive_write
        def fail_draft(path, text):
            if path.parent == self.drafts:
                raise OSError('模拟草稿写入失败')
            return real_write(path, text)
        with patch('knowledge_pipeline.cli.exclusive_write', side_effect=fail_draft):
            with self.assertRaises(PipelineError):
                self.run_prepare()
        failures = list(self.output.glob('failed-*/record.json'))
        self.assertEqual(len(failures), 1)
        self.assertEqual(json.loads(failures[0].read_text())['state'], 'failed')
        self.assertEqual(list(self.drafts.glob('*.md')), [])
        with self.assertRaises(PipelineError):
            self.run_prepare()

    def test_two_processes_prepare_one_draft(self):
        args = [sys.executable, '-m', 'knowledge_pipeline.cli', 'prepare',
                '--source', str(self.source), '--source-root', str(self.root),
                '--vault', str(self.vault), '--output', str(self.output),
                '--root-id', 'sample', '--permission-id', 'permit-1', '--allow-source-metadata']
        processes = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout)['state'])
        self.assertEqual(sorted(results), ['prepared', 'skipped'])

    def test_case_alias_output_is_rejected_on_case_insensitive_fs(self):
        alias = self.vault.with_name('VAULT')
        if not alias.exists():
            self.skipTest('临时文件系统区分大小写，不适用此项')
        with self.assertRaises(PipelineError):
            self.run_prepare(output=alias / 'private')

    def test_cli_inspect_and_prepare_end_to_end(self):
        command = [sys.executable, '-m', 'knowledge_pipeline.cli']
        common = ['--source', str(self.source), '--source-root', str(self.root)]
        inspect = subprocess.run(command + ['inspect'] + common, capture_output=True, text=True)
        self.assertEqual(inspect.returncode, 0, inspect.stderr)
        self.assertEqual(json.loads(inspect.stdout)['state'], 'inspected')
        args = ['prepare'] + common + ['--vault', str(self.vault), '--output', str(self.output),
                 '--root-id', 'sample', '--permission-id', 'permit-1', '--allow-source-metadata']
        first = subprocess.run(command + args, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)['state'], 'prepared')
        second = subprocess.run(command + args, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)['state'], 'skipped')


if __name__ == '__main__':
    unittest.main()
