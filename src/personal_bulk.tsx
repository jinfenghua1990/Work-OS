import {useState} from 'react'
import {sheetRows} from './finance_import'

type Field='paid_on'|'merchant'|'amount'|'payment_reference'|'claimant'|'category'|'business_line'|'project_code'|'memo'|'mode'|'existing_payable_id'
const fields:Field[]=[
  'paid_on','merchant','amount','payment_reference','claimant','category',
  'business_line','project_code','memo','mode','existing_payable_id',
]
const labels:Record<Field,string>={
  paid_on:'个人付款日期',merchant:'实际收款商家',amount:'付款金额',
  payment_reference:'个人交易流水号',claimant:'垫付人',category:'费用类别',
  business_line:'业务归属',project_code:'项目代码',memo:'用途说明',
  mode:'费用类型',existing_payable_id:'已入账应付ID',
}
const synonyms:Record<Field,string[]>={
  paid_on:['付款日期','交易日期','记账日期','日期','paid_on','date'],
  merchant:['商户','商家','收款方','对方户名','交易对方','merchant','counterparty'],
  amount:['金额','支出金额','交易金额','付款金额','amount'],
  payment_reference:['流水号','交易流水号','交易号','凭证号','支付单号','payment_reference','reference'],
  claimant:['垫付人','姓名','付款人','claimant'],
  category:['费用分类','费用类别','category'],
  business_line:['项目类型','业务类型','业务归属','business_line'],
  project_code:['项目代码','项目编号','project_code'],
  memo:['用途','摘要','备注','memo'],
  mode:['垫付类型','mode'],
  existing_payable_id:['原应付ID','原应付编号','existing_payable_id'],
}
type ImportSummary={
  created:{row:number;id:string}[];
  duplicates:{row:number;id:string}[];
  rejected:{row:number;error:string}[];
}
export function PersonalBulkImport({reload}:{reload:()=>void}){
  const [file,setFile]=useState<File|null>(null)
  const [headers,setHeaders]=useState<string[]>([])
  const [rows,setRows]=useState<string[][]>([])
  const [mapping,setMapping]=useState<Record<Field,string>>(
    Object.fromEntries(fields.map(field=>[field,''])) as Record<Field,string>,
  )
  const [claimant,setClaimant]=useState('')
  const [defaultCategory,setDefaultCategory]=useState('other')
  const [defaultBusiness,setDefaultBusiness]=useState('corporate')
  const [error,setError]=useState('')
  const [busy,setBusy]=useState(false)
  const [summary,setSummary]=useState<ImportSummary|null>(null)

  async function choose(input?:File){
    if(!input)return
    setFile(input);setError('');setRows([]);setHeaders([]);setSummary(null)
    try{
      const grid=await sheetRows(input)
      if(grid.length<2)throw new Error('清单必须包含表头和至少一条付款')
      const names=grid[0].map((name,index)=>name.trim().replace(/^\uFEFF/,'')||'列'+(index+1))
      setHeaders(names);setRows(grid.slice(1))
      setMapping(Object.fromEntries(fields.map(field=>[
        field,names.find(x=>synonyms[field].includes(x.trim().toLowerCase()))||''
      ])) as Record<Field,string>)
    }catch(reason){setError(reason instanceof Error?reason.message:'无法读取个人垫付清单')}
  }
  function cell(values:string[],field:Field){
    const index=mapping[field]?headers.indexOf(mapping[field]):-1
    return index<0?'':String(values[index]||'').trim()
  }
  const required:Field[]=['paid_on','merchant','amount','payment_reference']
  const ready=rows.length>0&&required.every(k=>Boolean(mapping[k]))&&
    (Boolean(mapping.claimant)||Boolean(claimant.trim()))
  async function upload(){
    if(!file||!ready){setError('请映射日期、商家、金额、交易号，并填写垫付人');return}
    setBusy(true);setError('');setSummary(null)
    try{
      const prepared=rows.map(row=>({
        claimant:cell(row,'claimant')||claimant.trim(),
        merchant:cell(row,'merchant'),
        paid_on:cell(row,'paid_on'),
        amount:cell(row,'amount').replace(/,/g,''),
        payment_reference:cell(row,'payment_reference'),
        mode:cell(row,'mode')||'expense',
        existing_payable_id:cell(row,'existing_payable_id')||null,
        category:cell(row,'category')||defaultCategory,
        business_line:cell(row,'business_line')||defaultBusiness,
        entity_code:'main',
        project_code:cell(row,'project_code')||null,
        memo:cell(row,'memo'),
        currency:'CNY',
      }))
      const period=prepared[0].paid_on.slice(0,7)
      if(!/^\d{4}-\d{2}$/.test(period)||
         prepared.some(row=>row.paid_on.slice(0,7)!==period)){
        throw new Error('请先按月份筛选付款，每份清单只导入同一月份')
      }
      const proof=await fetch('/api/archive/files',{method:'POST',
        headers:{'Content-Type':'application/octet-stream',
          'X-File-Name':encodeURIComponent(file.name),
          'X-Document-Category':'expense',
          'X-Document-Period':period,
          'X-Source-System':'finance.personal_advance.import'},
        body:file,
      })
      const archived=await proof.json()
      if(!proof.ok||!archived.id)throw new Error(archived.error||'个人付款清单原件归档失败')
      const result:ImportSummary={created:[],duplicates:[],rejected:[]}
      for(let from=0;from<prepared.length;from+=100){
        const response=await fetch('/api/personal-advances/import',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({archive_file_id:archived.id,rows:prepared.slice(from,from+100)}),
        })
        const data=await response.json()
        if(!response.ok)throw new Error(data.error||'个人垫付批次导入失败')
        for(const key of ['created','duplicates','rejected'] as const){
          const subset=data[key] as {row:number;id?:string;error?:string}[]
          if(key==='rejected'){
            result.rejected.push(...subset.map(x=>({row:x.row+from,error:x.error||'待复核'})))
          }else{
            result[key].push(...subset.map(x=>({row:x.row+from,id:x.id||''})))
          }
        }
      }
      setSummary(result);reload()
    }catch(reason){
      setError(reason instanceof Error?reason.message:'导入失败；可按相同清单重试，已登记数据不会重复生成')
    }finally{setBusy(false)}
  }
  return <div className="personal-bulk">
    <h3>批量导入个人垫付清单</h3>
    <p>只整理与公司有关的个人付款。CSV / XLSX 原文件留档，每一行先成为“待审核”，不自动生成费用或公账流水。</p>
    <div className="form-split">
      <label>选择付款清单<input type="file" accept=".csv,.xlsx"
        onChange={e=>void choose(e.target.files?.[0])}/></label>
      <label>默认垫付人<input value={claimant} onChange={e=>setClaimant(e.target.value)}
        placeholder="例如：本人姓名"/></label>
      <label>默认业务<select value={defaultBusiness}
        onChange={e=>setDefaultBusiness(e.target.value)}>
        <option value="coffee">咖啡</option><option value="bike">自行车</option>
        <option value="exhibition">展会</option><option value="corporate">公共费用</option>
      </select></label>
      <label>默认类别<select value={defaultCategory} onChange={e=>setDefaultCategory(e.target.value)}>
        <option value="other">其他费用</option>
        <option value="logistics">物流仓储</option><option value="travel">交通差旅</option>
        <option value="software">软件服务</option><option value="marketing">市场推广</option>
        <option value="event_fee">会议展会服务</option><option value="sample_cost">样品费用</option>
        <option value="accommodation">住宿</option>
      </select></label>
    </div>
    {headers.length>0&&<>
      <div className="personal-bulk-map">
        {fields.map(field=><label key={field}>{labels[field]}
          <select value={mapping[field]} onChange={e=>setMapping({...mapping,[field]:e.target.value})}>
            <option value="">不映射{required.includes(field)?'（必填）':''}</option>
            {headers.map((name,i)=><option key={i} value={name}>{name}</option>)}
          </select></label>)}
      </div>
      <div className="personal-bulk-submit"><strong>共 {rows.length} 条付款记录</strong>
        <button type="button" className="primary-button" disabled={busy||!ready}
          onClick={()=>void upload()}>{busy?'正在保存…':'归档原件并生成待审垫付'}</button></div>
    </>}
    {summary&&<p>新登记 {summary.created.length} 条，重复 {summary.duplicates.length} 条，
      待复核 {summary.rejected.length} 条。
      {summary.rejected.slice(0,10).map(row=><span key={row.row}> 第{row.row}行：{row.error}；</span>)}</p>}
    {error&&<p className="form-error">{error}</p>}
    <p>已经有采购应付的项目，请在清单中填写“垫付类型=existing_payable”及“原应付ID”，否则应单独人工确认，避免重复记成本。</p>
  </div>
}
