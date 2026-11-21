import {useEffect,useState} from 'react'

type CurrencyCash={
  currency:string;inflow:string;outflow:string;net:string;transfer_in:string;transfer_out:string;
  unallocated:string;transactions:number;unmatched:number;
}
type CurrencyInvoices={
  currency:string;input_count:number;output_count:number;
  input_total:string;output_total:string;pending_tax:number;
}
type MonthlyFlow={
  period:string;closed:boolean;original_uploads:number;scope:string;
  bank:{transaction_count:number;currencies:CurrencyCash[]};
  invoices:{document_count:number;currencies:CurrencyInvoices[]};
}

export function MonthlyFlowPanel(){
  const [period,setPeriod]=useState(()=>{
    const today=new Date()
    return String(today.getFullYear())+'-'+String(today.getMonth()+1).padStart(2,'0')
  })
  const [data,setData]=useState<MonthlyFlow|null>(null)
  const [loading,setLoading]=useState(false)
  const [error,setError]=useState('')
  useEffect(()=>{
    let live=true
    setLoading(true);setError('');setData(null)
    fetch('/api/reports/flow-summary?period='+encodeURIComponent(period),{cache:'no-store'})
      .then(async response=>{
        const value=await response.json()
        if(!response.ok)throw new Error(value.error||'报表读取失败')
        if(live)setData(value as MonthlyFlow)
      })
      .catch(err=>{if(live)setError(err instanceof Error?err.message:'报表读取失败')})
      .finally(()=>{if(live)setLoading(false)})
    return()=>{live=false}
  },[period])
  return <section className="panel finance-panel">
    <div className="panel-heading">
      <div><h2>月度资金与票据流水</h2><p>资金收支、进销项票据分开统计，不把银行到账当作销售收入。</p></div>
      <label>月份<input type="month" value={period} onChange={e=>setPeriod(e.target.value)}/></label>
    </div>
    {loading&&<p>读取中…</p>}
    {error&&<p className="form-error">{error}</p>}
    {data&&<>
      <div className="cash-flow-coverage-status">
        <strong>{data.closed?'已关账':'当月未关账'}</strong>
        <span>银行交易 {data.bank.transaction_count} 笔</span>
        <span>发票 {data.invoices.document_count} 张</span>
        <span>原文件归档记录 {data.original_uploads} 次</span>
      </div>
      <h3>银行资金流水</h3>
      <div className="data-table">
        <div className="data-head simple-flow-head"><span>币种</span><span>收款</span><span>付款</span><span>净流入</span><span>未认领</span></div>
        {data.bank.currencies.map(row=><div className="data-row simple-flow-row" key={row.currency}>
          <span>{row.currency}</span><span>{row.inflow}</span><span>{row.outflow}</span>
          <strong>{row.net}</strong><span>{row.unallocated}<small>待认领 {row.unmatched} 笔</small></span>
        </div>)}
        {!data.bank.currencies.length&&<div className="empty-inline">这个月没有已导入的银行流水。</div>}
      </div>
      <h3>进销项发票</h3>
      <div className="data-table">
        <div className="data-head simple-flow-head"><span>币种</span><span>进项合计</span><span>销项合计</span><span>进项数量</span><span>待核实</span></div>
        {data.invoices.currencies.map(row=><div className="data-row simple-flow-row" key={row.currency}>
          <span>{row.currency}</span><span>{row.input_total}</span><span>{row.output_total}</span>
          <span>{row.input_count} 张</span><span>{row.pending_tax} 张</span>
        </div>)}
        {!data.invoices.currencies.length&&<div className="empty-inline">这个月没有已登记的进销项发票。</div>}
      </div>
      <p>{data.scope}</p>
    </>}
  </section>
}
