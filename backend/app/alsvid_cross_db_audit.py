"""Read-only cross-database preflight for migrating ALSVID financial references.

This module cannot import, approve, settle, create a finance document, or
modify either database. ALSVID business facts remain in ALSVID. Work OS is
the intended single financial ledger, and no amount should be copied without
reviewing actual originals, open items and bank records.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

ZERO=Decimal("0")
MAX_DETAILS=100
ALLOWED_DIRECTIONS={"AP":"payable","AR":"receivable"}
PAYMENT_DIRECTIONS={"OUTBOUND":"out","INBOUND":"in"}


def _decimal(value:object)->Decimal:
    try:
        result=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError) as exc:
        raise ValueError("invalid amount in cross-database reconciliation") from exc
    if not result.is_finite() or result<0:
        raise ValueError("invalid amount in cross-database reconciliation")
    return result


def _normalize(value:object)->str:
    return str(value or "").strip().casefold()


def compare(alsvid:dict,workos:dict,*,as_of:date|None=None)->dict:
    """Produce a review list from explicit source IDs, never amount-only guesses."""
    day=as_of or date.today()
    obligations=list(alsvid.get("obligations") or [])
    payments=list(alsvid.get("payments") or [])
    docs=list(alsvid.get("finance_documents") or [])
    local_open=list(workos.get("open_items") or [])
    local_bank=list(workos.get("bank_transactions") or [])
    local_invoice=list(workos.get("invoices") or [])
    issues=[]
    def issue(code:str,source_id:str,detail:str,priority:str="review"):
        issues.append({"code":code,"source_id":source_id,
                       "detail":detail,"priority":priority})

    mapped=defaultdict(list)
    for row in local_open:
        if _normalize(row.get("source_type"))=="alsvid_finance_obligation":
            mapped[str(row.get("source_id") or "")].append(row)
    mapped_count=0
    missing_count=0
    mismatched_count=0
    total_by_currency=defaultdict(lambda:ZERO)
    for item in obligations:
        sid=str(item["id"])
        currency=str(item["currency"]).upper()
        value=_decimal(item["original_amount"])
        total_by_currency[currency]+=value
        work_rows=mapped.get(sid,[])
        direction=ALLOWED_DIRECTIONS.get(str(item.get("direction","")).upper())
        if not direction or value<=0:
            issue("invalid_alsvid_obligation",sid,
                  "义务类型或金额无效，不能直接迁移。","blocking")
            mismatched_count+=1
            continue
        if len(work_rows)>1:
            issue("duplicate_finance_mapping",sid,
                  "同一 ALSVID 义务已经对应多个 Work OS 往来。","blocking")
            mismatched_count+=1
            continue
        if work_rows:
            local=work_rows[0]
            if (_normalize(local.get("direction"))!=direction or
                str(local.get("currency","")).upper()!=currency or
                _decimal(local.get("original_amount"))!=value):
                issue("mapped_obligation_conflict",sid,
                      "已映射往来与 ALSVID 原币金额、方向或币种不一致；禁止自动调整。",
                      "blocking")
                mismatched_count+=1
                continue
            mapped_count+=1
            continue
        missing_count+=1
        if _normalize(item.get("source_type"))=="manual":
            issue("legacy_manual_obligation",sid,
                  "ALSVID 的 MANUAL 义务缺可验证业务凭据；先核实原采购/销售单及发生日期。")
        else:
            issue("obligation_unmapped",sid,
                  "该 ALSVID 义务未有 Work OS 唯一映射；需先核对业务与账务。")

    known_alsvid_ids={str(x["id"]) for x in obligations}
    for key,work_rows in mapped.items():
        if key not in known_alsvid_ids:
            issue("orphan_workos_mapping",key,
                  "Work OS 存在 ALSVID 义务映射，但在本次 ALSVID 来源数据未找到。","blocking")
            mismatched_count+=1

    # A legacy finance_payment is not necessarily an actual bank movement.
    # Match only by external bank reference + exact bank facts; never amount alone.
    by_reference=defaultdict(list)
    for row in local_bank:
        ref=_normalize(row.get("bank_reference"))
        if ref:
            by_reference[ref].append(row)
    matched_bank_count=0
    needs_payment_review=0
    future_count=0
    for payment in payments:
        sid=str(payment["id"])
        direction=PAYMENT_DIRECTIONS.get(str(payment.get("direction","")).upper())
        value=_decimal(payment["amount"])
        currency=str(payment["currency"]).upper()
        value_date=str(payment.get("value_date") or "")
        if not direction or value<=0:
            issue("invalid_alsvid_payment",sid,"付款方向或金额不正确。","blocking")
            needs_payment_review+=1
            continue
        if value_date>day.isoformat():
            future_count+=1
            issue("future_dated_alsvid_payment",sid,
                  "日期在核对日之后：不是可确认的已发生银行流水，禁止认作已支付。",
                  "blocking")
            needs_payment_review+=1
            continue
        ref=_normalize(payment.get("external_reference"))
        candidates=by_reference.get(ref,[]) if ref else []
        exact=[candidate for candidate in candidates if
               _normalize(candidate.get("direction"))==direction and
               str(candidate.get("currency","")).upper()==currency and
               _decimal(candidate.get("amount"))==value and
               str(candidate.get("transaction_on"))==value_date]
        if len(exact)==1 and len(candidates)==1:
            matched_bank_count+=1
            # Even an exact match still requires manual provenance review;
            # this program does not post or allocate bank entries.
        else:
            needs_payment_review+=1
            issue("payment_bank_unverified",sid,
                  "缺少同一银行流水号、日期、方向、原币金额的唯一匹配；先核对银行原件。")

    # financial_documents are NOT equivalent to verified input/output VAT
    # invoices. Count without turning their records into Work OS invoices.
    for item in docs:
        issue("finance_document_unclassified",str(item["id"]),
              "ALSVID 财务单据不能直接视为税务发票；需核实票据种类及发票原件。")
    invoice_source_ids=defaultdict(list)
    for row in local_invoice:
        if _normalize(row.get("source_system")) in (
            "alsvid","alsvid-admin","alsvid_admin"
        ):
            source_id=str(row.get("source_document_id") or "")
            if source_id:
                invoice_source_ids[source_id].append(row)
            else:
                issue("invoice_missing_source_identity",str(row.get("id")),
                      "Work OS 标注 ALSVID 的发票缺外部唯一票据 ID。",
                      "blocking")
    for key,rows in invoice_source_ids.items():
        if len(rows)>1:
            issue("invoice_duplicate_source",key,
                  "Work OS 出现相同 ALSVID 来源 ID 的多张发票。","blocking")

    counts={
        "alsvid_obligations":len(obligations),
        "workos_mapped_obligations":mapped_count,
        "workos_unmapped_obligations":missing_count,
        "obligation_mismatches":mismatched_count,
        "alsvid_finance_payments":len(payments),
        "bank_unique_reference_matches":matched_bank_count,
        "payments_requiring_review":needs_payment_review,
        "future_dated_payments":future_count,
        "alsvid_finance_documents":len(docs),
        "workos_alsvid_invoices":sum(len(x) for x in invoice_source_ids.values()),
    }
    issues.sort(key=lambda i:(0 if i["priority"]=="blocking" else 1,
                              i["code"],i["source_id"]))
    return {
        "status":"review_required" if issues else "no_known_differences",
        "read_only":True,
        "as_of":day.isoformat(),
        "source_authority":{"business":"alsvid","finance":"workos"},
        "counts":counts,
        "obligations_by_original_currency":[
            {"currency":currency,"amount":str(value)}
            for currency,value in sorted(total_by_currency.items())
        ],
        "issues":issues[:MAX_DETAILS],
        "issues_total":len(issues),
        "issues_truncated":len(issues)>MAX_DETAILS,
        "safe_to_auto_post":False,
        "note":"此检查不修改任一库。ALSVlD 历史付款可能是人工登记或未来日期，不能直接当银行流水；应收应付必须先审核原业务，再建立唯一来源映射。"
    }
