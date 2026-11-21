import {useEffect,useState} from 'react'
import {PersonalBulkImport} from './personal_bulk'

type Payable={
  id:string;direction:string;source_type:string;counterparty:string;
  original_amount:string;settled_amount:string;currency:string;
}
type Claim={
  id:string;source_key:string;mode:'expense'|'existing_payable';
  claimant:string;merchant:string;paid_on:string;category:string|null;
  payment_reference:string;amount:string;currency:string;status:'draft'|'approved';
  reimbursement_status:'pending_review'|'pending_reimbursement'|'partially_reimbursed'|'reimbursed';
  reimbursed_amount:string;unreimbursed_amount:string;employee_payable_id:string|null;
  archive_file_id:string;business_line:string;project_code:string|null;
}
const categories:{value:string;label:string}[]=[
  {value:'marketing',label:'市场推广'},{value:'rent',label:'租赁'},
  {value:'logistics',label:'物流仓储'},{value:'software',label:'软件服务'},
  {value:'travel',label:'交通差旅'},{value:'utilities',label:'水电通信'},
  {value:'professional_services',label:'专业服务'},
  {value:'booth_rental',label:'展位租赁'},
  {value:'event_fee',label:'会议/展会服务'},
  {value:'sample_cost',label:'展品样品费用'},
  {value:'decoration',label:'会场搭建'},{value:'accommodation',label:'住宿'},
  {value:'other',label:'其他费用'},
]
const reimbursementLabels:Record<Claim['reimbursement_status'],string>={
  pending_review:'待财务确认',pending_reimbursement:'待报销',
  partially_reimbursed:'部分已报销',reimbursed:'已报销',
}
const today=()=>new Date().toLocaleDateString('sv-SE')

export function PersonalReimbursementsPanel({
  openItems,refresh,
}:{openItems:Payable[];refresh:()=>Promise<void>}){
  const [claims,setClaims]=useState<Claim[]>([])
  const [sourceKey,setSourceKey]=useState(()=>crypto.randomUUID())
  const [mode,setMode]=useState<'expense'|'existing_payable'>('expense')
  const [chosenPayable,setChosenPayable]=useState('')
  const [file,setFile]=useState<File|null>(null)
  const [busy,setBusy]=useState(false)
  const [approveBusy,setApproveBusy]=useState('')
  const [error,setError]=useState('')
  const [approvalOn,setApprovalOn]=useState(today)
  const [reviewer,setReviewer]=useState('')
  const [refreshTick,setRefreshTick]=useState(0)

  async function load(){
    try{
      const response=await fetch('/api/personal-advances',{cache:'no-store'})
      const data=await response.json()
      if(!response.ok)throw new Error(data.error||'报销台账读取失败')
      setClaims(data as Claim[])
      setError('')
    }catch(reason){
      setError(reason instanceof Error?reason.message:'报销台账读取失败')
    }
  }
  useEffect(()=>{void load()},[refreshTick,openItems])
  const payableOptions=openItems.filter(item=>
    item.direction==='payable' && item.currency==='CNY' &&
    item.source_type!=='personal_advance' &&
    Number(item.original_amount)>Number(item.settled_amount)
  )
  const selected=payableOptions.find(item=>item.id===chosenPayable)
  async function create(event:React.FormEvent<HTMLFormElement>){
    event.preventDefault()
    if(!file){setError('必须上传个人付款原始凭证');return}
    const form=event.currentTarget
    const data=Object.fromEntries(new FormData(form).entries())
    setBusy(true);setError('')
    try{
      const paidOn=String(data.paid_on)
      const response=await fetch('/api/archive/files',{method:'POST',
        headers:{'Content-Type':'application/octet-stream',
          'X-File-Name':encodeURIComponent(file.name),
          'X-Document-Category':'expense',
          'X-Document-Period':paidOn.slice(0,7),
          'X-Source-System':'finance.personal_advance'},body:file})
      const original=await response.json()
      if(!response.ok||!original.id)throw new Error(original.error||'个人付款凭证归档失败')
      const payload={
        source_key:'personal:'+sourceKey,mode,
        claimant:data.claimant,merchant:mode==='existing_payable'?selected?.counterparty:data.merchant,
        paid_on:paidOn,category:mode==='expense'?data.category:null,
        existing_payable_id:mode==='existing_payable'?chosenPayable:null,
        amount:data.amount,currency:'CNY',
        business_line:data.business_line,entity_code:data.entity_code,
        project_code:data.project_code,payment_reference:data.payment_reference,
        archive_file_id:original.id,memo:data.memo,
      }
      const save=await fetch('/api/personal-advances',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
      const result=await save.json()
      if(!save.ok)throw new Error(result.error||'个人垫付未登记')
      setSourceKey(crypto.randomUUID());setFile(null);form.reset()
      setMode('expense');setChosenPayable('')
      setRefreshTick(t=>t+1)
    }catch(reason){setError(reason instanceof Error?reason.message:'登记失败')}
    finally{setBusy(false)}
  }
  async function approve(claim:Claim){
    if(!reviewer.trim()){setError('请填写财务确认操作人');return}
    setApproveBusy(claim.id);setError('')
    try{
      const response=await fetch('/api/personal-advances/approve',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({advance_id:claim.id,approved_by:reviewer.trim(),
          accounting_on:approvalOn}),
      })
      const result=await response.json()
      if(!response.ok)throw new Error(result.error||'审批失败')
      await refresh();setRefreshTick(t=>t+1)
    }catch(reason){setError(reason instanceof Error?reason.message:'确认失败')}
    finally{setApproveBusy('')}
  }
  const pending=claims.filter(c=>c.reimbursement_status!=='reimbursed')
  return <section className="panel finance-panel personal-advances">
    <div className="panel-heading"><div>
      <h2>个人垫付与公司报销</h2>
      <p>个人先付款并上传凭证 → 财务确认形成对个人应付 → 公司银行转账后在银行流水中核销。</p>
    </div><button className="secondary-button" type="button"
      onClick={()=>setRefreshTick(t=>t+1)}>刷新台账</button></div>
    <p className="personal-important">个人银行卡交易不导入公司银行流水池。已存在采购应付的款项请选“代付已有应付”，避免重复计成本。</p>
    <PersonalBulkImport reload={()=>setRefreshTick(t=>t+1)}/>
    <form className="personal-form" onSubmit={e=>void create(e)}>
      <div className="form-split"><label>支付类型
        <select value={mode} onChange={e=>{setMode(e.target.value as typeof mode);setChosenPayable('')}}>
          <option value="expense">个人垫付新费用</option>
          <option value="existing_payable">个人代付已有采购/费用应付</option>
        </select></label><label>垫付人<input name="claimant" required placeholder="报销收款人姓名"/></label></div>
      {mode==='expense'?<div className="form-split">
        <label>实际收款商家<input name="merchant" required placeholder="商户或供应商名称"/></label>
        <label>费用类别<select name="category" defaultValue="other">
          {categories.map(x=><option key={x.value} value={x.value}>{x.label}</option>)}
        </select></label>
      </div>:<label>对应已入账应付款（不要再记一次成本）
        <select value={chosenPayable} onChange={e=>setChosenPayable(e.target.value)} required>
          <option value="">选择已有采购/费用应付款</option>
          {payableOptions.map(row=><option key={row.id} value={row.id}>
            {row.counterparty} · 待付 ¥{(Number(row.original_amount)-Number(row.settled_amount)).toFixed(2)} · {row.source_type}
          </option>)}
        </select></label>}
      <div className="form-split">
        <label>个人实际付款日期<input name="paid_on" type="date" required defaultValue={today()}/></label>
        <label>垫付金额（CNY）<input name="amount" required inputMode="decimal" placeholder="0.00"/></label>
        <label>个人支付参考号<input name="payment_reference" required placeholder="个人付款流水号/交易号"/></label>
      </div>
      <div className="form-split">
        <label>业务归属<select name="business_line" defaultValue="corporate">
          <option value="coffee">咖啡</option>
          <option value="bike">自行车</option>
          <option value="exhibition">展会</option>
          <option value="corporate">公司公共费用</option>
        </select></label>
        <label>主体<input name="entity_code" defaultValue="main" required/></label>
        <label>项目代码（展会必填）<input name="project_code" placeholder="例如 EVENT-2026-001"/></label>
      </div>
      <div className="form-split">
        <label>个人付款原件<input type="file" required accept=".pdf,.png,.jpg,.jpeg,.xlsx,.csv"
          onChange={e=>setFile(e.target.files?.[0]||null)}/></label>
        <label>业务说明<input name="memo" placeholder="购买用途、订单编号等"/></label>
      </div>
      <button className="primary-button" type="submit" disabled={busy}>
        {busy?'正在保存原件…':'登记个人垫付（待审核）'}
      </button>
    </form>
    {error&&<p className="form-error">{error}</p>}
    <div className="personal-summary">
      <strong>垫付台账：{claims.length} 笔</strong>
      <span>未全部报销 {pending.length} 笔</span>
    </div>
    <div className="form-split">
      <label>财务审核操作人<input value={reviewer} onChange={e=>setReviewer(e.target.value)}
        placeholder="手动填写审核人姓名"/></label>
      <label>本次确认入账日期<input type="date" value={approvalOn}
        onChange={e=>setApprovalOn(e.target.value)}/></label>
    </div>
    <div className="personal-ledger">
      {!claims.length&&<p>目前没有个人垫付记录。</p>}
      {claims.map(claim=><div className="personal-ledger-row" key={claim.id}>
        <div><strong>{claim.claimant} · {claim.currency} {claim.amount}</strong>
          <small>{claim.paid_on} · {claim.merchant} · {claim.payment_reference}</small>
          <small>{claim.mode==='existing_payable'?'代付现有应付':'新费用'} ·
            {reimbursementLabels[claim.reimbursement_status]} · 未报销 {claim.currency} {claim.unreimbursed_amount}</small>
        </div>
        {claim.status==='draft'?
          <button className="secondary-button" type="button" disabled={Boolean(approveBusy)}
            onClick={()=>void approve(claim)}>{approveBusy===claim.id?'确认中…':'确认垫付入账'}</button>:
          <span className="personal-status">{reimbursementLabels[claim.reimbursement_status]}</span>}
      </div>)}
    </div>
    <p className="personal-important">报销打款后，在「财务中心 → 银行流水」中将公账付款认领至“垫付人应付账款”，即可自动更新未报销余额。发票另在发票管理关联原业务。</p>
  </section>
}
