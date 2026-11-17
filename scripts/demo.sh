#!/usr/bin/env bash
# End-to-end demonstration of the delivery guarantees.
#
#   docker compose up -d --build       # from the repository root
#   ./scripts/demo.sh
#
# The script only uses curl + python3 (for JSON pretty output) and talks to
# the three containers.  It is idempotent-ish: re-running it just adds more
# demo mail, which is also a useful way to watch duplicate-report protection.
set -euo pipefail

APP_A="${APP_A:-http://localhost:8000}"
APP_B="${APP_B:-http://localhost:8001}"
DEMO="${DEMO:-http://localhost:8081}"

step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
json() { python3 -m json.tool; }

wait_for() {
  local url="$1" name="$2" tries="${3:-60}"
  for _ in $(seq 1 "$tries"); do
    if curl -fsS "$url" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "timeout waiting for $name ($url)" >&2
  return 1
}

step "等待服务就绪"
wait_for "$DEMO/healthz" "demo-imap" 120
wait_for "$APP_A/healthz" "web-a" 120
wait_for "$APP_B/healthz" "web-b" 120

deliver() { curl -fsS -X POST "$DEMO/ctl/deliver" -H 'Content-Type: application/json' -d "$1"; echo; }

step "1. 投递一封含两个附件的邮件，由 web-a 采集"
deliver '{"subject":"演示-双附件","attachments":[
  {"filename":"DEMO-R1.csv","report_no":"DEMO-R1","sample_no":"S001","item":"ALT","value":"21"},
  {"filename":"DEMO-R2.csv","report_no":"DEMO-R2","sample_no":"S002","item":"GLU","value":"5.4"}]}'
curl -fsS -X POST "$APP_A/api/scan" | json

step "2. 人工已读（模拟有人在邮箱里先打开）仍会被采集"
deliver '{"subject":"演示-人工已读","attachments":[
  {"filename":"DEMO-R3.csv","report_no":"DEMO-R3","sample_no":"S003","item":"K","value":"4.1"}]}'
# 演示源没有单独的“设为已读”接口，这里直接用 IMAP 客户端改标记。
python3 - "$DEMO" "${IMAP_HOST:-localhost}" "${IMAP_PORT:-1143}" <<'PY'
import imaplib, sys, json, urllib.request
host, port = sys.argv[2], int(sys.argv[3])
try:
    cfg = json.load(urllib.request.urlopen(sys.argv[1] + "/api/state"))
    uids = [m["uid"] for m in cfg["messages"] if m["subject"] == "演示-人工已读"]
except Exception as exc:  # noqa: BLE001
    print("skip pre-read step:", exc)
    raise SystemExit(0)
c = imaplib.IMAP4(host, port)
c.login("lab", "lab-pass")
c.select("INBOX")
for uid in uids:
    c.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Seen)")
c.logout()
print("marked pre-read message(s):", uids)
PY
curl -fsS -X POST "$APP_B/api/scan" | json

step "3. 同号同原件复用：新邮件带同样的 DEMO-R1 附件"
deliver '{"subject":"演示-复用原件","attachments":[
  {"filename":"DEMO-R1.csv","report_no":"DEMO-R1","sample_no":"S001","item":"ALT","value":"21"}]}'
curl -fsS -X POST "$APP_A/api/scan" | json

step "4. 同号异文整封拒收（DEMO-R2 内容不同），且不会带入其中的新报告号"
deliver '{"subject":"演示-同号异文","attachments":[
  {"filename":"DEMO-R9.csv","report_no":"DEMO-R9","sample_no":"S009","item":"NEW","value":"1"},
  {"filename":"DEMO-R2-bad.csv","content":"报告号,样本号,项目,测值\nDEMO-R2,S999,OTHER,999\n"}]}'
curl -fsS -X POST "$APP_A/api/scan" | json
curl -fsS "$APP_A/api/messages?mark_state=marked" \
  | python3 -c 'import json,sys
rows=json.load(sys.stdin)
bad=[r for r in rows if r["decision"]=="rejected"]
print(json.dumps(bad[:2], ensure_ascii=False, indent=2))
assert not any("DEMO-R9" in (r.get("report_nos") or "") for r in rows), "R9 leaked"'

step "5. 标记后断连：STORE 成功但回执丢失，本地保持待标记"
curl -fsS -X POST "$DEMO/ctl/drop-store" -H 'Content-Type: application/json' -d '{"enabled":true}' >/dev/null
deliver '{"subject":"演示-标记后断连","attachments":[
  {"filename":"DEMO-R4.csv","report_no":"DEMO-R4","sample_no":"S004","item":"NA","value":"139"}]}'
curl -fsS -X POST "$APP_A/api/scan" | json
curl -fsS "$APP_A/api/status" | python3 -c 'import json,sys;print("pending:",json.load(sys.stdin)["stats"]["pending_marks"])'
step "5b. 页面/接口重试补标记"
curl -fsS -X POST "$APP_A/api/retry-marks" | json
curl -fsS "$APP_A/api/status" | python3 -c 'import json,sys
s=json.load(sys.stdin)["stats"]; print("pending:",s["pending_marks"],"marked:",s["marked"])'

step "6. 两实例并发：同一时刻只允许一个推进邮箱"
deliver '{"subject":"演示-并发","attachments":[
  {"filename":"DEMO-R5.csv","report_no":"DEMO-R5","sample_no":"S005","item":"BUN","value":"6.2"}]}'
(curl -fsS -o /tmp/scan-a.json -w "web-a: %{http_code}\n" -X POST "$APP_A/api/scan" &
 curl -fsS -o /tmp/scan-b.json -w "web-b: %{http_code}\n" -X POST "$APP_B/api/scan" &
 wait) || true
cat /tmp/scan-a.json 2>/dev/null | json || true
cat /tmp/scan-b.json 2>/dev/null | json || true

step "7. 邮箱重建：UIDVALIDITY 变化 + UID 1 被新邮件重用"
# 7a. 先制造一条“标记后断连”的待标记记录，它属于旧 UIDVALIDITY。
curl -fsS -X POST "$DEMO/ctl/drop-store" -H 'Content-Type: application/json' -d '{"enabled":true}' >/dev/null
deliver '{"subject":"演示-重建前待标记","attachments":[
  {"filename":"DEMO-R7.csv","report_no":"DEMO-R7","sample_no":"S007","item":"AST","value":"33"}]}'
curl -fsS -X POST "$APP_B/api/scan" | json
BEFORE_UIDVALIDITY=$(curl -fsS "$APP_B/api/status" | python3 -c 'import json,sys;print(json.load(sys.stdin)["mailbox_state"]["uidvalidity"])')
BEFORE_RAW=$(curl -fsS "$APP_A/api/messages?mark_state=pending" | python3 -c 'import json,sys;rows=json.load(sys.stdin);print(rows[0]["raw_sha256"] if rows else "none")')
echo "旧代 uidvalidity=$BEFORE_UIDVALIDITY，待标记原文=${BEFORE_RAW:0:16}…"

# 7b. 清空重建邮箱（UIDVALIDITY+1，UID 从 1 重新分配），再投递全新邮件。
curl -fsS -X POST "$DEMO/ctl/rebuild" -H 'Content-Type: application/json' -d '{"purge":true}' | json
deliver '{"subject":"演示-重建后新邮件-UID1","attachments":[
  {"filename":"DEMO-R6.csv","report_no":"DEMO-R6","sample_no":"S006","item":"AST","value":"44"}]}'
curl -fsS -X POST "$APP_B/api/scan" | json
curl -fsS "$APP_B/api/status" | python3 -c 'import json,sys
d=json.load(sys.stdin); print(json.dumps({"mailbox_state":d["mailbox_state"],"stats":d["stats"]}, ensure_ascii=False, indent=2))'

# 7c. 断言：旧待标记记录变成“旧代失效”，新 UID 1 是另一封邮件，且两代互不串用。
curl -fsS "$APP_A/api/messages?mark_state=stale" | python3 -c "import json,sys
rows=json.load(sys.stdin)
quarantined=[r for r in rows if r['raw_sha256']=='$BEFORE_RAW']
assert quarantined, 'expected the old pending mark to be quarantined as stale'
assert quarantined[0]['uidvalidity']==int('$BEFORE_UIDVALIDITY'), 'quarantined row must belong to old UIDVALIDITY'
print('旧代 pending -> stale:', [(r['uidvalidity'], r['uid'], r['raw_sha256'][:12]) for r in quarantined])"
curl -fsS "$APP_A/api/messages" | python3 -c "import json,sys
rows=json.load(sys.stdin)
new=[r for r in rows if r['uidvalidity']!=int('$BEFORE_UIDVALIDITY') and r['uid']==1]
assert new and new[0]['decision']=='accepted' and 'DEMO-R6' in new[0]['report_nos'], new
assert new[0]['raw_sha256'] != '$BEFORE_RAW', 'UID 1 must not inherit the old receipt'
print('新代 UID 1:', new[0]['decision'], new[0]['mark_state'], new[0]['report_nos'], new[0]['raw_sha256'][:12])"

step "8. 最终入库报告与标记状态"
curl -fsS "$APP_A/api/messages?mark_state=marked" | python3 -c 'import json,sys
rows=json.load(sys.stdin)
for r in sorted(rows, key=lambda x: (x["uidvalidity"], x["uid"]))[-10:]:
    print(r["uidvalidity"], r["uid"], r["decision"], r["mark_state"], r["report_nos"], r["reason"] or "")'

echo
echo "全部演示步骤完成。浏览器打开 $APP_A 或 $APP_B 查看页面。"
