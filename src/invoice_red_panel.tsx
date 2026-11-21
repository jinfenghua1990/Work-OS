import {useEffect,useState} from 'react'

type BlueInvoice={
  id:string;invoice_number:string;direction:'purchase'|'sales';counterparty:string;
  issued_on:string;total_amount:string;tax_amount:string;currency:string;
  net_amount?:string;credited_amount?:string;red_count?:number
}
type RedInvoice={
  id:string;invoice_number:string;blue_invoice_number:string;direction:'purchase'|'sales';
  counterparty:string;issued_on:string;total_amount:string;tax_amount:string;
  currency:string;reason:string;blue_tax_status:string
}
type TaxRule={
  rate_code:string;valid_from:string;valid_to:string|null;effective_valid_to?:string|null;rate_percent:string;
  description:string;source_reference:string;created_by:string
}
type Sender=(path:string,payload:unknown)=>Promise<unknown>
type Archiver=(file:File,category:string,period:string,sourceSystem?:string)=>Promise<{id:string}>
const reasonNames:Record<string,string>={
  return:'退货/退回',discount:'折让',reissue:'发票重开',correction:'开票更正'
}
async function getList<T>(path:string):Promise<T[]>{
  const response=await fetch(path,{cache:'no-store'})
  const result=await response.json()
  if(!response.ok)throw new Error(result.error||'读取失败')
  return result as T[]
}

export function InvoiceRedTaxControls({
  invoices,send,archiveOriginal,refresh
}:{
  invoices:BlueInvoice[];send:Sender;archiveOriginal:Archiver;refresh:()=>Promise<void>
}){
  const [reds,setReds]=useState<RedInvoice[]>([])
  const [rates,setRates]=useState<TaxRule[]>([])
  const [blueId,setBlueId]=useState('')
  const [redFile,setRedFile]=useState<File|null>(null)
  const [redBusy,setRedBusy]=useState(false)
  const [rateBusy,setRateBusy]=useState(false)
  const [message,setMessage]=useState('')
  const [error,setError]=useState('')
  const blue=invoices.find(item=>item.id===blueId)

  async function load(){
    try{
      const [redRows,rateRows]=await Promise.all([
        getList<RedInvoice>('/api/invoices/red'),
        getList<TaxRule>('/api/tax/rates')
      ])
      setReds(redRows);setRates(rateRows);setError('')
    }catch(e){setError(e instanceof Error?e.message:'票据与税率字典读取失败')}
  }
  useEffect(()=>{void load()},[])
  async function saveRed(event:React.FormEvent<HTMLFormElement>){
    event.preventDefault();setError('');setMessage('')
    const form=event.currentTarget
    if(!blue||!redFile){setError('请选择蓝票及上传红字发票原件');return}
    const data=Object.fromEntries(new FormData(form).entries())
    setRedBusy(true)
    try{
      const period=String(data.issued_on).slice(0,7)
      const original=await archiveOriginal(redFile,blue.direction==='purchase'?'input_invoice':'output_invoice',
        period,'finance.invoice.red')
      const response=await send('/api/invoices/red',{
        ...data,blue_invoice_id:blue.id,archive_file_id:original.id
      })
      if(!response){setError('原件已归档，但红票登记未成功；请核对页面错误后重试');return}
      const result=response as {link_review_required?:boolean;vat_review_required?:boolean}
      setMessage('红票已登记，票据净额已更新。'+
        (result.link_review_required?' 注意：已关联金额大于冲红后净额，请复核。':'')+
        (result.vat_review_required?' 注意：原蓝票已确认抵扣，须核验进项税转出。':''))
      form.reset();setBlueId('');setRedFile(null)
      await refresh();await load()
    }catch(e){setError(e instanceof Error?e.message:'红票登记失败')}finally{setRedBusy(false)}
  }
  async function saveRate(event:React.FormEvent<HTMLFormElement>){
    event.preventDefault();setError('');setMessage('')
    const form=event.currentTarget
    setRateBusy(true)
    try{
      const fields=Object.fromEntries(new FormData(form).entries())
      const result=await send('/api/tax/rates',fields)
      if(!result){setError('税率字典保存失败，请核对页面错误');return}
      setMessage('税率版本已记录，仅供核对，不自动进行进项税抵扣或记账。')
      form.reset();await load()
    }catch(e){setError(e instanceof Error?e.message:'税率登记失败')}finally{setRateBusy(false)}
  }
  return <div className="invoice-red-controls">
    <div className="panel finance-panel">
      <div className="panel-heading">
        <div><h2>红字发票与蓝票核对</h2>
          <p>红票必须上传原件、指定唯一蓝票；按红票日期留痕。票据净额变化不等于收入退款或税务抵扣冲销。</p>
        </div><button className="secondary-button" type="button" onClick={()=>void load()}>刷新</button>
      </div>
      <form className="invoice-form" onSubmit={e=>void saveRed(e)}>
        <label>关联蓝字发票
          <select value={blueId} onChange={e=>setBlueId(e.target.value)} required>
            <option value="">选择现有蓝票</option>
            {invoices.map(item=><option key={item.id} value={item.id}>
              {item.invoice_number} · {item.counterparty} · {item.currency} 剩余 {item.net_amount??item.total_amount}
            </option>)}
          </select>
        </label>
        {blue&&<p>蓝票 {blue.invoice_number} · {blue.direction==='purchase'?'进项':'销项'} ·
          原额 {blue.currency} {blue.total_amount} · 已冲红 {blue.credited_amount??'0.00'}</p>}
        <label>红字发票原件（PDF / OFD / XML）
          <input type="file" accept=".pdf,.ofd,.xml" required onChange={e=>setRedFile(e.target.files?.[0]||null)}/>
        </label>
        <div className="form-split">
          <label>红字号码<input name="invoice_number" required maxLength={160}/></label>
          <label>开具日期<input name="issued_on" type="date" min={blue?.issued_on} required/></label>
        </div>
        <div className="form-split">
          <label>红冲金额（正数）<input name="total_amount" inputMode="decimal" required/></label>
          <label>红冲税额（正数）<input name="tax_amount" inputMode="decimal" defaultValue="0.00" required/></label>
        </div>
        <label>红冲业务原因
          <select name="reason">
            <option value="return">退货/退回</option>
            <option value="discount">销售折让</option>
            <option value="reissue">仅更正重开发票</option>
            <option value="correction">其他开票更正</option>
          </select>
        </label>
        <label>不可重复的来源凭证键<input name="source_key" required maxLength={160} placeholder="开票方税号 + 红字号码"/></label>
        <button type="submit" className="primary-button" disabled={redBusy||!blue||!redFile}>
          {redBusy?'原件归档中…':'登记红字发票'}
        </button>
      </form>
      <div className="data-table">
        {reds.map(item=><div className="data-row" key={item.id}>
          <span><strong>红字 {item.invoice_number}</strong><small>对应蓝字 {item.blue_invoice_number}</small></span>
          <span>{item.counterparty}</span><span>{item.issued_on}</span>
          <span>{item.currency} -{item.total_amount}（税额 -{item.tax_amount}）</span>
          <span>{reasonNames[item.reason]||item.reason}</span>
        </div>)}
        {!reds.length&&<div className="empty-state">暂无登记的红字发票</div>}
      </div>
    </div>
    <div className="panel finance-panel">
      <div className="panel-heading"><div><h2>税率字典 · 历史版本</h2>
        <p>手工维护有来源依据的税率版本，不修改旧版，不据此自动认定可抵扣。</p></div></div>
      <form className="invoice-form" onSubmit={e=>void saveRate(e)}>
        <div className="form-split">
          <label>税率编码<input name="rate_code" required placeholder="VAT_STANDARD"/></label>
          <label>税率（%）<input name="rate_percent" inputMode="decimal" required placeholder="13"/></label>
        </div>
        <div className="form-split">
          <label>生效日期<input name="valid_from" type="date" required/></label>
          <label>失效日期（可选）<input name="valid_to" type="date"/></label>
        </div>
        <label>税率说明<input name="description" required/></label>
        <label>法律/税务来源依据<input name="source_reference" required placeholder="文件名称、文号或官方链接"/></label>
        <label>记录人<input name="created_by" required placeholder="具名操作人"/></label>
        <button className="secondary-button" disabled={rateBusy}>{rateBusy?'保存中…':'登记税率版本'}</button>
      </form>
      <div className="data-table">
        {rates.map(item=><div className="data-row" key={item.rate_code+item.valid_from}>
          <strong>{item.rate_code}</strong><span>{item.rate_percent}%</span>
          <span>{item.valid_from} — {item.effective_valid_to??item.valid_to??'持续有效'}</span>
          <span>{item.description}</span><small>{item.source_reference} · {item.created_by}</small>
        </div>)}
        {!rates.length&&<div className="empty-state">暂无税率版本；不自动填充未经核实的税率。</div>}
      </div>
    </div>
    {message&&<p>{message}</p>}
    {error&&<p className="form-error">{error}</p>}
  </div>
}
