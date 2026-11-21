import {useEffect,useState} from 'react'

type Money={currency:string;amount:string}
type FinanceIssue={
  code:string;title:string;detail:string;reference:string;
  priority:'blocking'|'attention'|'info';scope:string
}
type AlsvidOverview={
  period:string;read_only:boolean
  integration:{connected:boolean;status:string;last_verified_sync:string|null;
    contract_version:string;source_of_truth:string;note:string}
  business:{purchase_order_count:number|null;sales_order_count:number|null;
    inventory_count:number|null;business_metrics_verified:boolean;
    legacy_local_purchase_refs:number;legacy_local_sales_rows:number}
  financial:{
    cash_paid:Money[];cash_received:Money[];
    accounts_payable:Money[];accounts_receivable:Money[];
    bank_allocation_count:number;invoice_linked:Money[];
    invoices:{input:number;output:number;tax_pending:number;without_business_link:number;missing_original:number};
    personal_pending_approval:number;personal_pending_reimbursement:number;
    personal_outstanding_cny:string;
    ledger_reference_cny:{sales_revenue_cny:string;cost_of_sales_cny:string;
      operating_expenses_cny:string;reference_margin_cny:string}
    ledger_reference_complete:boolean;scope:string
  }
  exceptions:{total:number;blocking:number;items:FinanceIssue[];truncated:boolean}
  personal_reimbursements:{
    source_key:string;claimant:string;paid_on:string;
    amount:string;outstanding:string;status:string
  }[]
  sync_events:unknown[];sync_events_available:boolean
}

const tabs:Record<string,string>={
  overview:'经营总览',activity:'财务对账',actions:'异常中心',sync:'同步记录',
}
const money=(rows:Money[])=> rows.length?
  rows.map(row=>row.currency+' '+row.amount).join(' / '):'暂无关联数据'
const actionText=(status:string)=>({
  draft:'待审核',pending_reimbursement:'待报销',
  partially_reimbursed:'部分报销',reimbursed:'已报销',
}[status]||status)

export function AlsvidFinanceWorkspace({
  tab,period,onOpenFinance,
}:{
  tab:string;period:string;onOpenFinance:(section:'bank'|'invoices'|'items'|'archive'|'reports')=>void
}){
  const [data,setData]=useState<AlsvidOverview|null>(null)
  const [loading,setLoading]=useState(false)
  const [error,setError]=useState('')
  const [reload,setReload]=useState(0)
  useEffect(()=>{
    let active=true
    setLoading(true);setData(null);setError('')
    fetch('/api/alsvid/finance/overview?period='+encodeURIComponent(period),{cache:'no-store'})
      .then(async response=>{
        const payload=await response.json()
        if(!response.ok)throw new Error(payload.error||'财务数据获取失败')
        if(active)setData(payload as AlsvidOverview)
      })
      .catch(reason=>{
        if(active)setError(reason instanceof Error?reason.message:'财务接口不可用')
      })
      .finally(()=>{if(active)setLoading(false)})
    return()=>{active=false}
  },[period,reload])
  const current=tabs[tab]||tabs.overview
  const btn=(section:'bank'|'invoices'|'items'|'archive'|'reports',label:string)=>
    <button type="button" className="secondary-button" onClick={()=>onOpenFinance(section)}>{label}</button>
  return <div className="alsvid-finance-workspace">
    <section className="panel">
      <div className="panel-heading">
        <div><h2>ALSVID · {current}</h2>
          <p>自行车业务单据由 ALSVID Admin 管理；银行、发票、报销和财务核销归 Work OS。</p>
        </div>
        <button type="button" className="secondary-button" disabled={loading}
          onClick={()=>setReload(n=>n+1)}>{loading?'读取中…':'刷新财务数据'}</button>
      </div>
      {error&&<p className="form-error">读取失败：{error}。此页不会使用演示值。</p>}
      {!error&&data&&<div className="alsvid-sync-note">
        <strong>{data.integration.connected?'已核验业务连接':'ALSVID 业务接口未接通'}</strong>
        <span>{data.integration.note}</span>
      </div>}
      {!error&&!data&&loading&&<p>正在读取 Work OS 已归集的财务记录…</p>}
    </section>
    {!data?null:tab==='overview'?<>
      <div className="alsvid-metrics">
        <div className="panel"><small>已关联银行付款（按币种）</small><strong>{money(data.financial.cash_paid)}</strong>
          <span>仅计算已核销至 ALSVID 往来的金额</span></div>
        <div className="panel"><small>已关联银行收款（按币种）</small><strong>{money(data.financial.cash_received)}</strong>
          <span>不等于当期销售收入</span></div>
        <div className="panel"><small>已归集未付应付款</small><strong>{money(data.financial.accounts_payable)}</strong>
          <span>Work OS 已识别的 ALSVID 往来</span></div>
        <div className="panel"><small>已归集应收款</small><strong>{money(data.financial.accounts_receivable)}</strong>
          <span>不包含未接入的 ALSVID 业务</span></div>
      </div>
      <section className="panel">
        <div className="panel-heading"><div><h2>业务汇总</h2>
          <p>未接入 ALSVID 的业务数据不会显示为零。</p></div></div>
        <div className="alsvid-summary-grid">
          <div><small>采购订单</small><strong>{data.business.purchase_order_count??'未接入'}</strong></div>
          <div><small>销售订单</small><strong>{data.business.sales_order_count??'未接入'}</strong></div>
          <div><small>实时库存</small><strong>{data.business.inventory_count??'未接入'}</strong></div>
          <div><small>经营利润</small><strong>待 ALSVID 成本数据接入</strong></div>
        </div>
        <p className="alsvid-subnote">历史 Work OS 自行车采购引用 {data.business.legacy_local_purchase_refs} 条、销售记录 {data.business.legacy_local_sales_rows} 条：仅作历史核对，不算 ALSVID 实时经营数据。</p>
      </section>
      <div className="alsvid-half">
        <section className="panel"><h2>财务待办</h2>
          <div className="alsvid-summary-grid">
            <div><small>需处理异常</small><strong>{data.exceptions.total}</strong></div>
            <div><small>个人待审批</small><strong>{data.financial.personal_pending_approval}</strong></div>
            <div><small>个人待报销</small><strong>{data.financial.personal_pending_reimbursement}</strong></div>
            <div><small>待确认发票税务状态</small><strong>{data.financial.invoices.tax_pending}</strong></div>
          </div>
          <p className="alsvid-subnote">个人垫付待报销：CNY {data.financial.personal_outstanding_cny}。处理入口统一在财务中心。</p>
        </section>
        <section className="panel"><h2>Work OS 已记账参考（CNY）</h2>
          <div className="alsvid-summary-grid">
            <div><small>收入</small><strong>{data.financial.ledger_reference_cny.sales_revenue_cny}</strong></div>
            <div><small>销售成本</small><strong>{data.financial.ledger_reference_cny.cost_of_sales_cny}</strong></div>
            <div><small>经营费用</small><strong>{data.financial.ledger_reference_cny.operating_expenses_cny}</strong></div>
            <div><small>参考损益</small><strong>{data.financial.ledger_reference_cny.reference_margin_cny}</strong></div>
          </div>
          <p className="alsvid-subnote">仅历史已归集分录，非完整 ALSVID 月度利润，不能据此做最终经营结论。</p>
        </section>
      </div>
    </>:tab==='activity'?<>
      <section className="panel"><h2>银行、应收应付、发票对照</h2>
        <div className="alsvid-summary-grid">
          <div><small>银行已核销</small><strong>{data.financial.bank_allocation_count} 次</strong></div>
          <div><small>已关联财务发票</small><strong>{data.financial.invoices.input+data.financial.invoices.output} 张</strong></div>
          <div><small>尚未关联业务的发票</small><strong>{data.financial.invoices.without_business_link} 张</strong></div>
          <div><small>已关联发票缺原件</small><strong>{data.financial.invoices.missing_original} 张</strong></div>
        </div>
        <p className="alsvid-subnote">按同一业务唯一来源 ID 关联；银行付款不会再次生成采购成本，发票不代表已付款。</p>
        <div className="alsvid-buttons">
          {btn('bank','查看银行流水')}
          {btn('items','查看应收应付')}
          {btn('invoices','查看发票')}
          {btn('archive','查看原始凭证')}
        </div>
      </section>
      <section className="panel"><h2>个人垫付与后续报销</h2>
        <p>个人支付不属于公司银行流水。审核后形成个人应付，公司转账报销时从 Work OS 银行流水核销个人应付。</p>
        {data.personal_reimbursements.length?data.personal_reimbursements.map(row=>
          <div className="alsvid-exception-row" key={row.source_key}>
            <span><strong>{row.claimant}</strong><small>{row.paid_on} · {row.source_key}</small></span>
            <span>CNY {row.amount} · 未报销 {row.outstanding}</span>
            <em>{actionText(row.status)}</em>
          </div>
        ):<p>暂无已标记为 ALSVID 的个人垫付。</p>}
      </section>
    </>:tab==='actions'?<>
      <section className="panel"><div className="panel-heading"><div>
        <h2>异常与核对待办</h2>
        <p>没有来源、存在差额或关联缺失的记录不会被标记为已完成。</p>
      </div><strong>{data.exceptions.total} 项</strong></div>
        {data.exceptions.items.map((item,index)=><div className="alsvid-exception-row" key={item.code+item.reference+index}>
          <span><strong>{item.title}</strong>
            <small>{item.detail}{item.reference?' · '+item.reference:''}</small>
          </span>
          <em>{item.priority==='blocking'?'需优先处理':'待核对'}</em>
        </div>)}
        {data.exceptions.truncated&&<p className="alsvid-subnote">异常过多，当前仅显示前 100 条，不能据此认为其他异常已消失。</p>}
        <div className="alsvid-buttons">
          {btn('bank','去银行核对')}{btn('invoices','去发票管理')}
          {btn('items','去应收应付')}{btn('reports','查看财务报表')}
        </div>
      </section>
    </>:<>
      <section className="panel"><h2>ALSVID 对接预留</h2>
        <div className="alsvid-summary-grid">
          <div><small>业务接口</small><strong>尚未连接</strong></div>
          <div><small>最近验证同步</small><strong>{data.integration.last_verified_sync??'无'}</strong></div>
          <div><small>业务权威来源</small><strong>ALSVID Admin</strong></div>
          <div><small>财务权威来源</small><strong>Work OS</strong></div>
        </div>
        <p className="alsvid-subnote">预留：采购单、交付入库、销售订单、应收应付事件同步；Work OS 回传银行核销、发票关联及报销状态。每条记录需业务系统、来源单 ID、版本及幂等键。</p>
        <p className="alsvid-subnote">未配置并验证服务端鉴权及 ALSVID API 前，不接收跨系统写入，不显示虚构同步日志，也不自动迁移历史财务数据。</p>
        <div className="alsvid-buttons">
          {btn('archive','检查原始凭证')}{btn('reports','查看财务报表')}
        </div>
      </section>
    </>}
  </div>
}
