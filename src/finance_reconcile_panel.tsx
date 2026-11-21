import {useEffect,useState} from 'react'

type AccountCheck={
  account_id:string;account:string;currency:string;
  status:'statement_missing'|'balance_difference'|'matched';
  opening_balance:string|null;closing_balance:string|null;
  calculated_closing:string|null;difference:string|null;
  inflow:string;outflow:string;allocated:string;remaining:string;
  posting_gap_cny:string;transaction_count:number;
  unallocated_count:number;unclassified_count:number;
  missing_evidence_count:number;posting_gap_count:number;
}
type BankReviewRow={transaction_id:string;date:string;account:string;
  currency:string;reference:string;description:string;remaining:string}
type InvoiceReview={id:string;number:string;counterparty:string;
  direction:'purchase'|'sales';currency:string;face_value:string;
  linked_amount:string;remaining:string;has_original:boolean}
type FinanceCheck={
  period:string;status:'no_accounts'|'review_required'|'balanced_with_pending_items'|'balanced';
  accounts:AccountCheck[];unknown_accounts:{account:string;currency:string}[];
  account_inventory_confirmed:boolean;
  bank:{
    transactions:number;unallocated_count:number;unclassified_allocations:number;
    missing_originals:number;posting_error_count:number;
    unallocated_rows:BankReviewRow[];
    posting_errors:{type:string;id:string;difference_cny?:string;missing_fx?:boolean}[];
  };
  invoice:{
    total:number;unlinked:number;partially_linked:number;
    tax_pending:number;missing_evidence:number;details:InvoiceReview[];
  };
  scope:string;
}

const checkLabels:Record<FinanceCheck['status'],string>={
  no_accounts:'尚未登记银行账户',
  review_required:'尚有差异需要处理',
  balanced_with_pending_items:'资金账核平，仍有待核实票据/暂挂项目',
  balanced:'银行、认领和本期票据核对无已知差异',
}

export function FinanceReconciliationPanel({
  currentPeriod,bankRows,
}:{currentPeriod:string;bankRows:unknown[]}){
  const [period,setPeriod]=useState(currentPeriod)
  const [data,setData]=useState<FinanceCheck|null>(null)
  const [busy,setBusy]=useState(false)
  const [error,setError]=useState('')
  const [refreshId,setRefreshId]=useState(0)
  useEffect(()=>setPeriod(currentPeriod),[currentPeriod])
  useEffect(()=>{
    let active=true
    setBusy(true);setError('');setData(null)
    fetch('/api/bank/reconciliation-summary?period='+encodeURIComponent(period),{cache:'no-store'})
      .then(async response=>{
        const output=await response.json()
        if(!response.ok)throw new Error(output.error||'对账数据读取失败')
        if(active)setData(output as FinanceCheck)
      })
      .catch(e=>{if(active)setError(e instanceof Error?e.message:'读取失败')})
      .finally(()=>{if(active)setBusy(false)})
    return()=>{active=false}
  },[period,refreshId,bankRows])
  return <section className="panel finance-panel finance-reconcile-panel">
    <div className="panel-heading"><div>
      <h2>月度三方对账</h2>
      <p>银行流水 ↔ 采购/销售应收应付 ↔ 发票；同时核对银行会计分录</p>
    </div><div className="reconcile-actions">
      <input aria-label="对账月份" type="month" value={period}
        onChange={event=>setPeriod(event.target.value)}/>
      <button type="button" className="secondary-button" disabled={busy}
        onClick={()=>setRefreshId(n=>n+1)}>{busy?'核对中…':'重新核对'}</button>
    </div></div>
    {error&&<p className="form-error">{error}</p>}
    {data&&<>
      <p className="reconcile-status"><strong>{checkLabels[data.status]}</strong>
        <span>银行 {data.bank.transactions} 笔 · 发票 {data.invoice.total} 张</span></p>
      <div className="reconcile-stats">
        <div><small>未认领银行流水</small><strong>{data.bank.unallocated_count} 笔</strong></div>
        <div><small>银行分录差异</small><strong>{data.bank.posting_error_count} 笔</strong></div>
        <div><small>未关联/部分关联发票</small><strong>{data.invoice.unlinked+data.invoice.partially_linked} 张</strong></div>
        <div><small>缺少原始凭证</small><strong>{data.bank.missing_originals+data.invoice.missing_evidence} 笔/张</strong></div>
      </div>
      {!data.account_inventory_confirmed&&<p className="reconcile-notice">
        银行账户完整性尚未确认，请在下方“银行账户对账”登记并确认账户清单。
      </p>}
      {data.unknown_accounts.length>0&&<p className="form-error">
        存在未登记账户或币种不符的银行流水：
        {data.unknown_accounts.map(row=>row.account+'（'+row.currency+'）').join('、')}
      </p>}
      <h3>逐个银行账户核对期初、收支、期末</h3>
      <div className="reconcile-table-scroll"><table className="reconcile-table">
        <thead><tr>
          <th>银行账户</th><th>期初</th><th>流入</th><th>流出</th>
          <th>计算期末</th><th>对账单期末</th><th>差额</th><th>核对结果</th>
        </tr></thead><tbody>
          {data.accounts.map(item=><tr key={item.account_id}>
            <td><strong>{item.account}</strong><small>{item.currency}</small></td>
            <td>{item.opening_balance??'未录入'}</td>
            <td>{item.inflow}</td><td>{item.outflow}</td>
            <td>{item.calculated_closing??'—'}</td>
            <td>{item.closing_balance??'未录入'}</td>
            <td>{item.difference??'—'}</td>
            <td>{item.status==='matched'?'余额核平':item.status==='statement_missing'?'缺对账单余额':'余额不符'}</td>
          </tr>)}
        </tbody></table></div>
      {!data.accounts.length&&<p>暂无已登记银行账户。</p>}
      {(data.bank.unallocated_rows.length>0||data.bank.unclassified_allocations>0)&&<>
        <h3>待认领与暂挂流水</h3>
        {data.bank.unallocated_rows.slice(0,12).map(row=><div key={row.transaction_id} className="reconcile-detail">
          <span>{row.date} · {row.account}<small>{row.reference} · {row.description}</small></span>
          <strong>{row.currency} {row.remaining}</strong>
        </div>)}
        {data.bank.unclassified_allocations>0&&<p>暂挂待分类 {data.bank.unclassified_allocations} 笔，尚未形成明确的业务归属。</p>}
      </>}
      {data.bank.posting_errors.length>0&&<>
        <h3>账务分录需复核</h3>
        {data.bank.posting_errors.slice(0,10).map(row=><p key={row.id}>
          流水 {row.id.slice(0,12)}：银行科目与核销的人民币金额差额 {row.difference_cny??'待核实'}。
        </p>)}
      </>}
      {(data.invoice.unlinked>0||data.invoice.partially_linked>0||data.invoice.tax_pending>0)&&
        <p className="reconcile-notice">进销项发票：未关联 {data.invoice.unlinked} 张、
          部分关联 {data.invoice.partially_linked} 张、税务状态待核实 {data.invoice.tax_pending} 张。
          票据关联不是付款，也不代表税务抵扣。</p>}
      {data.invoice.details.slice(0,10).map(item=><div key={item.id} className="reconcile-detail">
        <span>{item.direction==='purchase'?'进项':'销项'} · {item.number} · {item.counterparty}
          <small>{item.has_original?'原件已关联':'缺少已关联原件'} ·
            已匹配业务单据 {item.currency} {item.linked_amount}</small></span>
        <strong>未关联 {item.currency} {item.remaining}</strong>
      </div>)}
      <p className="reconcile-footnote">{data.scope}</p>
    </>}
  </section>
}
