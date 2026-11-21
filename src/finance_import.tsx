import {useEffect,useState} from 'react'

type BankRow={id:string;description:string;amount:string;remaining_amount:string}
type BankMatch={bank_transaction_id:string;bank_reference:string;transaction_on:string;description:string;target_id:string;target_type:'receivable'|'payable';counterparty:string;currency:string;amount:string;requires_confirmation:boolean}
type Sender=(path:string,payload:unknown)=>Promise<unknown>

export function BankMatchSuggestions({send,bankRows}:{send:Sender;bankRows:BankRow[]}){
  const [matches,setMatches]=useState<BankMatch[]>([])
  const [error,setError]=useState('')
  const [loading,setLoading]=useState(false)
  const [posting,setPosting]=useState('')
  async function load(){
    setLoading(true);setError('')
    try{
      const response=await fetch('/api/bank/match-suggestions',{cache:'no-store'})
      const data=await response.json()
      if(!response.ok)throw new Error(data.error||'匹配建议读取失败')
      setMatches(data.suggestions as BankMatch[])
    }catch(e){setError(e instanceof Error?e.message:'读取失败')}finally{setLoading(false)}
  }
  useEffect(()=>{void load()},[bankRows])
  async function approve(item:BankMatch){
    setPosting(item.bank_transaction_id);setError('')
    try{
      const saved=await send('/api/bank/allocate',{bank_transaction_id:item.bank_transaction_id,
        allocations:[{target_type:item.target_type,target_id:item.target_id,amount:item.amount}]})
      if(saved)await load()
    }catch(e){setError(e instanceof Error?e.message:'核销失败')}finally{setPosting('')}
  }
  return <section className="panel finance-panel">
    <div className="panel-heading"><div><h2>自动匹配建议</h2><p>金额、币种、往来单位一致才会推荐；确认前不会入账。</p></div>
      <button className="secondary-button" disabled={loading} onClick={()=>void load()}>{loading?'正在匹配…':'重新匹配'}</button></div>
    {error&&<p className="form-error">{error}</p>}
    {!matches.length&&!loading&&<p>暂无可以确定的匹配，其他流水仍在待认领列表。</p>}
    {matches.map(item=><div className="bank-match-row" key={item.bank_transaction_id}>
      <div><strong>{item.transaction_on} · {item.currency} {item.amount}</strong>
        <small>{item.bank_reference} · {item.description}</small>
        <small>{item.target_type==='payable'?'应付':'应收'} · {item.counterparty}</small></div>
      <button className="secondary-button" disabled={Boolean(posting)} onClick={()=>void approve(item)}>
        {posting===item.bank_transaction_id?'核销中…':'确认核销'}</button>
    </div>)}
  </section>
}

type InvoiceField='invoice_number'|'direction'|'counterparty'|'issued_on'|'total_amount'|'tax_amount'|'currency'|'exchange_rate'|'rate_date'
const names:Record<InvoiceField,string>={
  invoice_number:'发票号码',direction:'进/销项',counterparty:'往来单位',issued_on:'开票日期',
  total_amount:'价税合计',tax_amount:'税额',currency:'币种',exchange_rate:'汇率',rate_date:'汇率日期',
}
const aliases:Record<InvoiceField,string[]>={
  invoice_number:['发票号码','发票号','票号','invoice_number','invoice no'],
  direction:['方向','发票类型','类型','进销项','direction','invoice_type'],
  counterparty:['往来单位','开票方','供应商','购方名称','销方名称','交易对方','counterparty','supplier'],
  issued_on:['开票日期','日期','issued_on','invoice_date','date'],
  total_amount:['价税合计','含税金额','合计金额','总金额','total_amount','total'],
  tax_amount:['税额','税金','合计税额','tax_amount','tax'],
  currency:['币种','currency'],exchange_rate:['汇率','exchange_rate'],rate_date:['汇率日期','rate_date'],
}
const keys=Object.keys(names) as InvoiceField[]
const required:InvoiceField[]=['invoice_number','counterparty','issued_on','total_amount','tax_amount']

function csvCells(text:string):string[][]{
  const rows:string[][]=[];let row:string[]=[];let cell='';let quoted=false
  for(let i=0;i<text.length;i++){
    const ch=text[i]
    if(ch==='"'&&quoted&&text[i+1]==='"'){cell+='"';i++}
    else if(ch==='"'){quoted=!quoted}
    else if(ch===','&&!quoted){row.push(cell);cell=''}
    else if((ch==='\n'||ch==='\r')&&!quoted){
      if(ch==='\r'&&text[i+1]==='\n')i++
      row.push(cell);if(row.some(v=>v.trim()))rows.push(row);row=[];cell=''
    }else cell+=ch
  }
  row.push(cell);if(row.some(v=>v.trim()))rows.push(row)
  return rows
}

export async function sheetRows(file:File):Promise<string[][]>{
  if(/\.csv$/i.test(file.name))return csvCells(await file.text())
  if(!/\.xlsx$/i.test(file.name))throw new Error('只支持 CSV、XLSX')
  const response=await fetch('/api/import/preview-xlsx',{method:'POST',
    headers:{'Content-Type':'application/octet-stream'},body:file})
  const body=await response.json()
  if(!response.ok)throw new Error(body.error||'Excel 解析失败')
  return body.rows as string[][]
}

export function InvoiceBatchImport({refresh}:{refresh:()=>Promise<void>}){
  const [file,setFile]=useState<File|null>(null)
  const [headers,setHeaders]=useState<string[]>([])
  const [rows,setRows]=useState<string[][]>([])
  const [mapping,setMapping]=useState<Record<InvoiceField,string>>({
    invoice_number:'',direction:'',counterparty:'',issued_on:'',total_amount:'',
    tax_amount:'',currency:'',exchange_rate:'',rate_date:'',
  })
  const [defaultDirection,setDefaultDirection]=useState<'purchase'|'sales'>('purchase')
  const [defaultCurrency,setDefaultCurrency]=useState('CNY')
  const [error,setError]=useState('')
  const [busy,setBusy]=useState(false)
  const [outcome,setOutcome]=useState<{created:number;duplicates:number;rejected:{row:number;error:string}[]}|null>(null)

  async function choose(next?:File){
    if(!next)return
    setFile(next);setRows([]);setHeaders([]);setError('');setOutcome(null)
    try{
      const grid=await sheetRows(next)
      if(grid.length<2)throw new Error('清单必须有表头和明细')
      const head=grid[0].map((v,index)=>v.trim().replace(/^\uFEFF/,'')||'列'+String(index+1))
      setHeaders(head);setRows(grid.slice(1))
      setMapping(Object.fromEntries(keys.map(key=>[
        key,head.find(h=>aliases[key].includes(h.trim().toLowerCase()))||''
      ])) as Record<InvoiceField,string>)
    }catch(e){setError(e instanceof Error?e.message:'清单解析失败')}
  }
  function get(cells:string[],field:InvoiceField):string{
    const index=mapping[field]?headers.indexOf(mapping[field]):-1
    return index>=0?(cells[index]||'').trim():''
  }
  const ready=required.every(field=>Boolean(mapping[field]))&&rows.length>0

  async function submit(){
    if(!file||!ready){setError('请先完成必填字段的映射');return}
    setError('');setOutcome(null);setBusy(true)
    try{
      const period=get(rows[0],'issued_on').slice(0,7)
      if(!/^\d{4}-\d{2}$/.test(period))throw new Error('开票日期应使用 YYYY-MM-DD')
      const original=await fetch('/api/archive/files',{method:'POST',
        headers:{'Content-Type':'application/octet-stream',
          'X-File-Name':encodeURIComponent(file.name),
          'X-Document-Category':'other','X-Document-Period':period,
          'X-Source-System':'finance.invoice.import'},body:file})
      const evidence=await original.json()
      if(!original.ok||!evidence.id)throw new Error(evidence.error||'原始清单归档失败')
      const items=rows.map(cells=>{
        const currency=(get(cells,'currency')||defaultCurrency).toUpperCase()
        return {
          invoice_number:get(cells,'invoice_number'),
          direction:get(cells,'direction')||defaultDirection,
          counterparty:get(cells,'counterparty'),
          issued_on:get(cells,'issued_on'),
          total_amount:get(cells,'total_amount'),tax_amount:get(cells,'tax_amount'),
          currency,
          exchange_rate:get(cells,'exchange_rate')||(currency==='CNY'?'1':''),
          rate_date:get(cells,'rate_date')||(currency==='CNY'?get(cells,'issued_on'):''),
        }
      })
      const total:{created:number;duplicates:number;rejected:{row:number;error:string}[]}={
        created:0,duplicates:0,rejected:[],
      }
      for(let offset=0;offset<items.length;offset+=100){
        const response=await fetch('/api/invoices/import',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({archive_file_id:evidence.id,rows:items.slice(offset,offset+100)})})
        const body=await response.json()
        if(!response.ok)throw new Error(body.error||'批量导入失败')
        total.created+=body.created.length;total.duplicates+=body.duplicates.length
        total.rejected.push(...body.rejected.map((item:{row:number;error:string})=>({
          row:item.row+offset,error:item.error,
        })))
      }
      setOutcome(total);await refresh()
    }catch(e){setError(e instanceof Error?e.message:'导入失败；已写入记录可以安全重试')}
    finally{setBusy(false)}
  }
  return <section className="panel finance-panel">
    <div className="panel-heading"><div><h2>每月进销项发票导入</h2>
      <p>原始 CSV / XLSX 完整归档，重复单据自动识别，发票抵扣状态保持待核实。</p></div></div>
    <div className="form-split">
      <label>选择原始发票清单<input type="file" accept=".csv,.xlsx" onChange={e=>void choose(e.target.files?.[0])}/></label>
      <label>默认类型<select value={defaultDirection} onChange={e=>setDefaultDirection(e.target.value as 'purchase'|'sales')}>
        <option value="purchase">进项</option><option value="sales">销项</option></select></label>
      <label>默认币种<input value={defaultCurrency} maxLength={3} onChange={e=>setDefaultCurrency(e.target.value.toUpperCase())}/></label>
    </div>
    {headers.length>0&&<><div className="invoice-batch-mapping">
      {keys.map(key=><label key={key}>{names[key]}
        <select value={mapping[key]} onChange={e=>setMapping({...mapping,[key]:e.target.value})}>
          <option value="">不映射{required.includes(key)?'（必选）':''}</option>
          {headers.map(column=><option key={column} value={column}>{column}</option>)}
        </select></label>)}
    </div><div className="invoice-batch-actions"><strong>共 {rows.length} 行</strong>
      <button type="button" className="primary-button" disabled={!ready||busy} onClick={()=>void submit()}>
        {busy?'正在保存…':'保存原件并导入'}</button></div></>}
    {outcome&&<p>新增 {outcome.created}，重复 {outcome.duplicates}，待复核 {outcome.rejected.length}。
      {outcome.rejected.slice(0,10).map(item=><span key={item.row}> 第{item.row+1}行：{item.error}；</span>)}</p>}
    {error&&<p className="form-error">{error}</p>}
  </section>
}
