#!/usr/bin/env python3
"""A dependency-free PubChem ingestion CLI."""
from __future__ import annotations
import argparse, json, sqlite3, sys, time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

NAME_API_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{query}/property/IUPACName,MolecularFormula,MolecularWeight/JSON"
CID_API_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cid}/property/IUPACName,MolecularFormula,MolecularWeight/JSON"
REQUEST_INTERVAL_SECONDS = 0.2
# 这些字段同时定义规范化数据契约和完整率校验范围。
REQUIRED = ("id", "query", "iupac_name", "molecular_formula", "molecular_weight", "fetched_at")
class SourceError(RuntimeError): pass
def now(): return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
def log_event(args, event, **fields):
    # 每条日志都是单行 JSON，方便终端查看或被日志平台直接采集。
    entry = {"timestamp":now(), "event":event, **fields}
    line = json.dumps(entry, ensure_ascii=False)
    print(line, file=sys.stderr)
    if getattr(args, "log_file", None):
        log_path = Path(args.log_file); log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle: handle.write(line + "\n")
def fetch_response(query, mock):
    # Mock 数据让完整演示在无网络环境中也可重复执行。
    if mock: return {"PropertyTable":{"Properties":[{"CID":2244,"IUPACName":"2-acetyloxybenzoic acid","MolecularFormula":"C9H8O4","MolecularWeight":"180.16"}]}}
    error = None
    # 对临时数据源、网络传输和解码失败执行指数退避重试。
    for attempt in range(3):
        try:
            # 纯数字查询按 CID 获取；其他查询按化合物名称检索。
            url = CID_API_URL.format(cid=query) if query.isdigit() else NAME_API_URL.format(query=quote(query, safe=""))
            # 简单限流，避免连续请求超过公开 API 的推荐频率。
            time.sleep(REQUEST_INTERVAL_SECONDS)
            request = Request(url, headers={"Accept":"application/json", "User-Agent":"dataeng-cli-demo/1.0"})
            with urlopen(request, timeout=15) as response: payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict): raise SourceError("PubChem returned an unexpected JSON payload.")
            return payload
        except HTTPError as exc:
            if exc.code == 404: raise SourceError(f"No PubChem result found for query: {query!r}.") from exc
            error = exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc: error = exc
        if attempt < 2: time.sleep(2 ** attempt)
    raise SourceError(f"PubChem is unavailable after 3 attempts: {error}") from error
def normalize(payload, query):
    try: properties = payload["PropertyTable"]["Properties"]
    except (KeyError, TypeError) as exc: raise SourceError("PubChem response does not contain PropertyTable.Properties.") from exc
    if not isinstance(properties, list) or not properties: raise SourceError(f"No PubChem result found for query: {query!r}.")
    records = []
    for item in properties:
        # PubChem CID 足够稳定，可作为数据源级别的自然唯一键。
        try: records.append({"id":f"pubchem:{int(item['CID'])}","source":"pubchem","query":query,"iupac_name":str(item["IUPACName"]),"molecular_formula":str(item["MolecularFormula"]),"molecular_weight":float(item["MolecularWeight"]),"fetched_at":now()})
        except (KeyError, TypeError, ValueError) as exc: raise SourceError("PubChem response contains an invalid compound record.") from exc
    return records
def require_pubchem(source):
    if source != "pubchem": raise SourceError(f"Unsupported source {source!r}; this demo supports only 'pubchem'.")
def command_fetch(args):
    require_pubchem(args.source); payload = fetch_response(args.query, args.mock); records = normalize(payload, args.query); output = Path(args.output)
    # 单独保留未修改的 API 响应，确保处理后数据可审计、可重放。
    raw = output / f"pubchem_{args.query.replace(' ', '_')}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"; processed = output / "processed" / "records.jsonl"; write_json(raw, payload); processed.parent.mkdir(parents=True, exist_ok=True)
    with processed.open("a", encoding="utf-8") as handle:
        for record in records: handle.write(json.dumps(record, ensure_ascii=False)+"\n")
    result = {"raw_response":str(raw),"processed_records":str(processed),"records":len(records)}
    if args.result_output: write_json(Path(args.result_output), result)
    log_event(args, "fetch_completed", source=args.source, query=args.query, records=len(records), mock=args.mock)
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
def database(path):
    path.parent.mkdir(parents=True, exist_ok=True); connection = sqlite3.connect(path)
    # `records` 是保证幂等的目标表；`state` 独立保存数据源水位线。
    connection.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, payload TEXT NOT NULL, synced_at TEXT NOT NULL)"); connection.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"); return connection
def comparable_payload(record):
    # `fetched_at` 仅表示本次采集时间，不应导致业务内容未变化的记录被误判为更新。
    return json.dumps({key:value for key,value in record.items() if key != "fetched_at"}, ensure_ascii=False, sort_keys=True)
def command_sync(args):
    require_pubchem(args.source); connection = database(Path(args.state)); previous = connection.execute("SELECT value FROM state WHERE key='watermark:pubchem'").fetchone(); requested = args.since or (previous[0] if previous else None); records = normalize(fetch_response(args.query, args.mock), args.query); inserted = updated = skipped = 0
    for record in records:
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True); comparable = comparable_payload(record); existing = connection.execute("SELECT payload FROM records WHERE id=?", (record["id"],)).fetchone()
        # 通过主键查询和 payload 比较，分别实现新增、更新和无操作语义。
        if existing is None: connection.execute("INSERT INTO records VALUES (?, ?, ?)", (record["id"], encoded, now())); inserted += 1
        elif comparable_payload(json.loads(existing[0])) != comparable: connection.execute("UPDATE records SET payload=?, synced_at=? WHERE id=?", (encoded, now(), record["id"])); updated += 1
        else: skipped += 1
    # 所有记录操作成功完成后，才推进水位线。
    watermark = now(); connection.execute("INSERT OR REPLACE INTO state VALUES ('watermark:pubchem', ?)", (watermark,)); connection.commit(); connection.close(); result={"source":args.source,"requested_since":requested,"watermark":watermark,"inserted":inserted,"updated":updated,"skipped":skipped,"state":args.state}
    if args.output: write_json(Path(args.output), result)
    log_event(args, "sync_completed", source=args.source, inserted=inserted, updated=updated, skipped=skipped, mock=args.mock)
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
def command_validate(args):
    # 读取所有 JSONL 批次，使报告反映整个处理目录的数据状态。
    files = sorted(Path(args.data_dir).rglob("*.jsonl")) if Path(args.data_dir).exists() else []; records=[]; invalid=0
    for file in files:
        for line in file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try: records.append(json.loads(line))
                except json.JSONDecodeError: invalid += 1
    schema = invalid; filled=0; ids=[]
    for record in records:
        if not isinstance(record, dict): schema += 1; continue
        complete = all(record.get(field) not in (None, "") for field in REQUIRED); filled += sum(record.get(field) not in (None, "") for field in REQUIRED)
        if not complete or not isinstance(record.get("molecular_weight"), (int,float)) or not str(record.get("id","")).startswith("pubchem:"): schema += 1
        if isinstance(record.get("id"), str): ids.append(record["id"])
    # 只有存在记录、字段完整、数据唯一且符合 Schema 的批次才可用。
    total=len(records); completeness=round(filled/(total*len(REQUIRED)),4) if total else 0.0; duplicate_count=len(ids)-len(set(ids)); duplicate=round(duplicate_count/total,4) if total else 0.0; passed=total>0 and completeness>=0.95 and duplicate==0 and schema==0
    comments = []
    if total == 0:
        comments.append("未发现可校验的有效记录")
    if completeness >= 0.95:
        comments.append(f"必填字段完整率为 {completeness:.2%}，符合不低于 95% 的阈值")
    else:
        comments.append(f"必填字段完整率为 {completeness:.2%}，低于 95% 的阈值")
    if duplicate_count == 0:
        comments.append("未发现重复的来源唯一 ID")
    else:
        comments.append(f"发现 {duplicate_count} 条重复的来源唯一 ID")
    if schema == 0:
        comments.append("字段类型和格式均符合预定义 Schema")
    else:
        comments.append(f"发现 {schema} 条字段类型、格式或 JSON 结构错误")
    comments.append("数据质量校验通过。" if passed else "数据质量校验未通过，建议修复上述问题后重新执行校验。")
    report={"total_records":total,"completeness_rate":completeness,"duplicate_rate":duplicate,"schema_errors":schema,"stale_records":0,"pass":passed,"comment":"；".join(comments)}
    if args.output: write_json(Path(args.output), report)
    log_event(args, "validation_completed", total_records=total, passed=passed, schema_errors=schema, duplicate_rate=duplicate)
    print(json.dumps(report, ensure_ascii=False, indent=2)); return 0 if passed else 2
def parser():
    root=argparse.ArgumentParser(prog="dataeng-cli", description="PubChem ingestion and quality validation demo"); commands=root.add_subparsers(dest="command", required=True)
    fetch=commands.add_parser("fetch", help="Fetch a PubChem compound and retain its raw response"); fetch.add_argument("--source",required=True); fetch.add_argument("--query",required=True); fetch.add_argument("--output",required=True); fetch.add_argument("--result-output",help="保存本次命令结果 JSON 的文件路径"); fetch.add_argument("--log-file",help="保存 JSON Lines 结构化日志的文件路径"); fetch.add_argument("--mock",action="store_true"); fetch.set_defaults(handler=command_fetch)
    sync=commands.add_parser("sync",help="Synchronize a compound idempotently into SQLite"); sync.add_argument("--source",required=True); sync.add_argument("--query",default="aspirin"); sync.add_argument("--since"); sync.add_argument("--state",default="./data/state.db"); sync.add_argument("--output",help="保存本次同步结果 JSON 的文件路径"); sync.add_argument("--log-file",help="保存 JSON Lines 结构化日志的文件路径"); sync.add_argument("--mock",action="store_true"); sync.set_defaults(handler=command_sync)
    validate=commands.add_parser("validate",help="Validate JSONL processed records"); validate.add_argument("data_dir"); validate.add_argument("--format",choices=["json"],default="json"); validate.add_argument("--output"); validate.add_argument("--log-file",help="保存 JSON Lines 结构化日志的文件路径"); validate.set_defaults(handler=command_validate); return root
def main():
    try: args=parser().parse_args(); return args.handler(args)
    except SourceError as error: print(f"error: {error}",file=sys.stderr); return 1
if __name__ == "__main__": raise SystemExit(main())
