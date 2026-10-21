"""显式单文件转换和来源草稿准备；不调用生成模型。"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

LEARNING = Path('05-学习笔记/AI学习')
FORMATS = {'.md', '.txt', '.pdf', '.docx', '.pptx'}


class PipelineError(Exception):
    """可以直接呈现的业务错误。"""


def checked_path(value):
    """拒绝路径链中的软链接，避免写入边界被重定向。"""
    p = Path(os.path.abspath(value))
    for part in [p, *p.parents]:
        if part.is_symlink():
            raise PipelineError('路径及其父目录不得是符号链接。')
    return p


def inside(path, root):
    if path == root or root in path.parents:
        return True
    if root.exists():
        for parent in [path, *path.parents]:
            if parent.exists() and os.path.samefile(parent, root):
                return True
    return False


def frontmatter(text):
    """解析约定的顶层标量，无法判断来源身份时停止而非跳过。"""
    lines = text.lstrip('\ufeff').splitlines()
    if not lines or lines[0] != '---':
        raise PipelineError('来源记录缺少 frontmatter，请人工核查。')
    try:
        end = lines.index('---', 1)
    except ValueError as exc:
        raise PipelineError('来源记录缺少完整结束标记。') from exc
    values = {}
    for line in lines[1:end]:
        match = re.fullmatch(r'([a-z][a-z0-9_]*):\s*(.*?)\s*', line)
        if not match:
            continue
        key, value = match.groups()
        if key in values:
            raise PipelineError('来源记录包含重复元数据字段。')
        if value.startswith('"'):
            try:
                value, offset = json.JSONDecoder().raw_decode(value)
                tail = match.group(2)[offset:].strip()
                if tail and not tail.startswith('#'):
                    raise ValueError('invalid tail')
            except ValueError as exc:
                raise PipelineError('来源记录的引号或注释格式异常。') from exc
        elif value.startswith("'"):
            quoted = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
            if not quoted:
                raise PipelineError('来源记录的单引号格式异常。')
            value = quoted.group(1).replace("''", "'")
        else:
            value = value.split(' #', 1)[0].strip()
        values[key] = value
    if not re.fullmatch(r'[a-fA-F0-9]{64}', values.get('sha256', '')):
        raise PipelineError('来源记录缺少有效 sha256，停止查重。')
    values['sha256'] = values['sha256'].lower()
    return values


def failure_record(output, digest, permission_id, message):
    folder = Path(tempfile.mkdtemp(prefix='failed-', dir=output))
    exclusive_write(folder / 'record.json', json.dumps({
        'sha256': digest, 'permission_id': permission_id, 'state': 'failed',
        'execution': 'local', 'model_called': False, 'error': message
    }, ensure_ascii=False, indent=2) + '\n')


def convert(source):
    """Office/PDF 使用隔离子进程内的本地转换接口。"""
    if source.suffix.lower() in {'.md', '.txt'}:
        try:
            result = source.read_text(encoding='utf-8-sig')
        except UnicodeError as exc:
            raise PipelineError('文本需要 UTF-8 编码，请人工另存后重试。') from exc
        converter = {'name': 'utf8', 'version': 'stdlib'}
    else:
        code = (
            'import sys, json, importlib.metadata; '
            'from markitdown import MarkItDown; '
            'r=MarkItDown(enable_plugins=False).convert_local(sys.argv[1]); '
            'print(json.dumps({"text":r.text_content,"version":'
            'importlib.metadata.version("markitdown")}, ensure_ascii=False))'
        )
        try:
            run = subprocess.run([sys.executable, '-c', code, str(source)],
                                 capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired as exc:
            raise PipelineError('本地转换超过 90 秒，未生成草稿。') from exc
        if run.returncode:
            raise PipelineError('本地转换失败，请检查格式和独立环境中的 MarkItDown 依赖。')
        try:
            data = json.loads(run.stdout)
            result = data['text']
            converter = {'name': 'markitdown', 'version': data['version']}
        except (ValueError, KeyError) as exc:
            raise PipelineError('转换结果格式异常，未生成草稿。') from exc
    result = result.replace('\r\n', '\n').replace('\r', '\n')
    if not result.strip():
        raise PipelineError('未提取到文字；扫描件或空文件需要人工处理。')
    if len(result.encode('utf-8')) > 20 * 1024 * 1024:
        raise PipelineError('转换文字超过 20 MB，本次停止。')
    return result, converter


def source_info(source, root):
    source, root = checked_path(source), checked_path(root)
    if not root.is_dir() or not source.is_file() or not inside(source, root):
        raise PipelineError('源文件必须是指定资料根目录内的普通文件。')
    if source.suffix.lower() not in FORMATS:
        raise PipelineError('仅接受 MD、TXT、文字 PDF、DOCX、PPTX。')
    if source.stat().st_size > 10 * 1024 * 1024:
        raise PipelineError('源文件超过 10 MB，请先人工拆分。')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return source, source.relative_to(root).as_posix(), digest


def sources(learning):
    """只查来源记录元数据；拒绝软链接子目录或文件。"""
    for base in [learning / '90-资料来源索引', learning / '_staging/来源草稿']:
        checked_path(base)
        if not base.is_dir():
            raise PipelineError('来源索引或来源草稿目录不存在，请先建立骨架。')
        for directory, dirs, files in os.walk(base, followlinks=False):
            for name in dirs + files:
                p = checked_path(Path(directory) / name)
                if p.is_file() and p.suffix == '.md':
                    try:
                        meta = frontmatter(p.read_text(encoding='utf-8-sig'))
                    except PipelineError as exc:
                        raise PipelineError('来源元数据无法核查：' + str(p) + '；' + str(exc)) from exc
                    yield p, meta


def exclusive_write(path, text):
    with path.open('x', encoding='utf-8', newline='\n') as f:
        f.write(text)


def prepare(source, root, vault, output, root_id, permission_id, allow_metadata=False):
    if not allow_metadata:
        raise PipelineError('需要明确允许将资料相对路径写入来源草稿。')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', root_id):
        raise PipelineError('root-id 只允许小写英文、数字与连字符，最多 64 字符。')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', permission_id):
        raise PipelineError('permission-id 应为私人许可记录编号，不填写许可全文。')
    source, relative, digest = source_info(source, root)
    vault, output = checked_path(vault), checked_path(output)
    learning = checked_path(vault / LEARNING)
    draft_dir = checked_path(learning / '_staging/来源草稿')
    if not vault.is_dir() or not draft_dir.is_dir():
        raise PipelineError('Vault 或来源草稿目录不存在。')
    if inside(output, vault) or inside(vault, output) or (output / '.git').exists():
        raise PipelineError('私人输出目录必须在 Vault 外，且不能是 Git 仓库。')
    for parent in output.parents:
        if (parent / '.git').exists():
            raise PipelineError('私人输出目录不得位于 Git 仓库中。')
    output.mkdir(parents=True, exist_ok=True)
    # 锁放系统临时目录，不把运行状态写进 Vault；同一用户同 Vault 串行。
    identity = vault.stat()
    lock_name = hashlib.sha256(f'{identity.st_dev}:{identity.st_ino}'.encode()).hexdigest()
    lock_path = Path(tempfile.gettempdir()) / ('ai-knowledge-' + lock_name + '.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = list(sources(learning))
        matches = [(p, m) for p, m in prior if m.get('sha256') == digest]
        if len(matches) > 1:
            raise PipelineError('已有多个相同哈希来源，请人工确认，未写入。')
        if matches and matches[0][1].get('state') == 'failed':
            raise PipelineError('已有失败来源记录，请人工核查并处理后再重试。')
        if matches:
            return {'state': 'skipped', 'sha256': digest, 'message': '相同资料已有来源记录，未重复转换或创建草稿。'}
        source_id = 'src-' + digest
        if any(m.get('id') == source_id for _, m in prior):
            raise PipelineError('已有来源使用相同 id 但哈希不一致，请人工核查。')
        destination = draft_dir / (source_id + '.md')
        if destination.exists():
            raise PipelineError('草稿目标已存在，禁止覆盖。')
        changed = any(m.get('root_id') == root_id and m.get('relative_path') == relative for _, m in prior)
        title = '来源-' + digest[:12]
        fields = {'title': title, 'type': 'note', 'id': source_id, 'note_kind': 'source',
                  'root_id': root_id, 'relative_path': relative, 'sha256': digest,
                  'file_type': source.suffix[1:].lower(), 'state': 'converted'}
        # 日期来自本地上海时区，机器时间由调用环境负责。
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        fields['created'] = fields['updated'] = today
        try:
            text, converter = convert(source)
        except PipelineError as exc:
            failure_record(output, digest, permission_id, str(exc))
            raise
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            failure_record(output, digest, permission_id, '转换期间源文件变化')
            raise PipelineError('转换期间源文件变化，未创建草稿，请人工重试。')
        artifact = output / digest
        if artifact.exists():
            raise PipelineError('私人转换结果已存在；核查无有效来源后，将哈希目录人工改名备份再重试。')
        try:
            artifact.mkdir(mode=0o700)
            direct = source.suffix.lower() in {'.md', '.txt'}
            text_path = source if direct else artifact / 'converted.md'
            record = {'source_id': source_id, 'sha256': digest, 'permission_id': permission_id,
                      'converter': converter, 'execution': 'local', 'model_called': False,
                      'text_mode': 'source_reference' if direct else 'converted_file',
                      'text_path': str(text_path),
                      'source_path': str(source), 'draft_path': str(destination), 'state': 'conversion_ready'}
            if not direct:
                exclusive_write(text_path, text)
            exclusive_write(artifact / 'record.json', json.dumps(record, ensure_ascii=False, indent=2) + '\n')
            front = '---\n' + '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False) for k, v in fields.items())
            note = front + '\ntags: [AI, 来源]\nnote_ids: []\nsupersedes:\n---\n\n# ' + title
            note += '\n\n## 转换与起草记录\n\n- 私人记录编号：' + digest
            note += '\n- 转换器：' + converter['name'] + '；版本：' + converter['version']
            note += ('\n- 已校验文本，直接引用原件，不另存全文。' if direct else '\n- 已完成本地转换，全文保存在私人目录。')
            note += '未调用起草模型、未审核、未验证页码或表格。\n'
            note += '\n## 人工合并建议\n\n' + ('同路径出现不同内容，请核查来源版本，不自动关联 supersedes。' if changed else '尚无自动合并建议；改名且改内容的来源需人工确认。') + '\n'
            exclusive_write(destination, note)
            record['state'] = 'prepared'
            # 状态先保存意图；私有记录更新失败时不会覆盖来源或重复创建。
            temporary_record = artifact / 'record-complete.json'
            exclusive_write(temporary_record, json.dumps(record, ensure_ascii=False, indent=2) + '\n')
            temporary_record.replace(artifact / 'record.json')
        except OSError as exc:
            try:
                failure_record(output, digest, permission_id, '写入阶段失败：' + str(exc))
            except OSError:
                raise PipelineError('写入失败且无法保存失败记录，请核查私人输出目录和草稿。') from exc
            raise PipelineError('写入阶段失败，已保存私人失败记录；核查部分结果后重试。') from exc
        return {'state': 'prepared', 'sha256': digest, 'source_draft': str(destination),
                'private_record': str(artifact / 'record.json'), 'needs_review': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description='手动本地资料整理；不调用模型、不发布、不导入。')
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect', help='只读检查单份资料和哈希')
    prep = sub.add_parser('prepare', help='本地转换并独占新建来源草稿')
    for p in [inspect, prep]:
        p.add_argument('--source', required=True, help='明确指定源文件')
        p.add_argument('--source-root', required=True, help='获准资料根目录')
    for name in ['vault', 'output', 'root-id', 'permission-id']:
        prep.add_argument('--' + name, required=True)
    prep.add_argument('--allow-source-metadata', action='store_true', help='确认允许相对路径进入来源草稿')
    args = parser.parse_args(argv)
    try:
        if args.command == 'inspect':
            _, relative, digest = source_info(args.source, args.source_root)
            result = {'state': 'inspected', 'relative_path': relative, 'sha256': digest}
        else:
            result = prepare(args.source, args.source_root, args.vault, args.output,
                             args.root_id, args.permission_id, args.allow_source_metadata)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PipelineError, OSError, UnicodeError) as exc:
        print('整理失败：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
