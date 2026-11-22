import {useEffect,useState} from 'react'

type Review={
  id:string;action_type:'bank_allocation'|'manual_journal'|'invoice_tax_status';
  status:'pending'|'approved'|'cancelled';source_key:string;
  payload:string;result_payload:string|null;
  submitted_by:string;submitted_at:string;
  approved_by:string|null;approved_at:string|null;
  cancelled_by:string|null;cancelled_at:string|null
}
type Session={named_accounts?:boolean;username?:string|null;role?:string|null}
const names:Record<Review['action_type'],string>={
  bank_allocation:'银行流水认领 / 核销',
  manual_journal:'手工调整凭证',
  invoice_tax_status:'发票税务状态确认'
}

async function invoke(path:string,method:'GET'|'POST'='GET',body?:unknown){
  const response=await fetch(path,{
    method,cache:'no-store',
    headers:method==='POST'?{'Content-Type':'application/json'}:undefined,
    body:method==='POST'?JSON.stringify(body):undefined
  })
  const data=await response.json()
  if(!response.ok)throw new Error(data.error||'财务审批操作失败')
  return data
}

export function FinanceActionApprovals({refresh}:{refresh:()=>Promise<void>}){
  const [actor,setActor]=useState<Session|null>(null)
  const [rows,setRows]=useState<Review[]>([])
  const [busy,setBusy]=useState('')
  const [error,setError]=useState('')
  const [notice,setNotice]=useState('')
  async function reload(){
    try{
      const [auth,result]=await Promise.all([
        invoke('/api/auth/status'),invoke('/api/finance/reviews')
      ])
      setActor(auth as Session)
      setRows(result.requests as Review[]||[])
      setError('')
    }catch(e){setError(e instanceof Error?e.message:'无法读取财务审批')}
  }
  useEffect(()=>{void reload()},[])
  async function change(path:string,row:Review){
    setError('');setNotice('');setBusy(row.id)
    try{
      await invoke(path,'POST',{request_id:row.id})
      setNotice((names[row.action_type])+(path.endsWith('/approve')
        ?' 已复核入账，审批及过账记录已保存'
        :' 已撤回待审，保留原申请记录'))
      await Promise.all([reload(),refresh()])
    }catch(e){setError(e instanceof Error?e.message:'操作失败')}
    finally{setBusy('')}
  }
  const canApprove=actor?.role==='owner'||actor?.role==='finance_approver'
  return <section className="panel finance-panel">
    <div className="panel-heading">
      <div><h2>财务高风险操作审批</h2>
        <p>银行认领、手工分录与发票税务确认，申请人和审批人必须是不同的具名账号；批准之前不执行过账。</p>
      </div>
      <button className="secondary-button" type="button" onClick={()=>void reload()}>刷新清单</button>
    </div>
    {!actor?.named_accounts&&<p>当前单人模式不启用此项审批。</p>}
    {error&&<p className="form-error" role="alert">{error}</p>}
    {notice&&<p role="status">{notice}</p>}
    <div className="data-table">
      {rows.map(row=>{
        const isOwn=row.submitted_by===actor?.username
        const pending=row.status==='pending'
        const allowed=pending&&canApprove&&!isOwn
        const canCancel=pending&&(isOwn||actor?.role==='owner')
        let payload:Record<string,unknown>={}
        try{payload=JSON.parse(row.payload) as Record<string,unknown>}catch{payload={}}
        const summary=row.action_type==='bank_allocation'
          ? '流水 '+String(payload.bank_transaction_id||'')+' · '+(Array.isArray(payload.allocations)?payload.allocations.length:0)+' 项认领'
          :row.action_type==='manual_journal'
          ? String(payload.description||'')+' · '+String(payload.posted_on||'')
          : '发票 '+String(payload.invoice_id||'')+' · '+String(payload.tax_status||'')
        return <article className="data-row" key={row.id}>
          <div>
            <strong>{names[row.action_type]} · {row.status==='pending'?'待复核':row.status==='approved'?'已批准':'已撤回'}</strong>
            <small>{summary}</small>
            <small>申请人：{row.submitted_by} · {row.submitted_at.slice(0,19).replace('T',' ')}</small>
            <details><summary>查看原始审批内容</summary>
              <pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',maxWidth:'48rem'}}>
                {JSON.stringify(payload,null,2)}
              </pre>
            </details>
          </div>
          <span>{row.status==='approved'
            ?'审批人：'+(row.approved_by||'—')
            :isOwn?'等待另一账号审核':'待处理'}</span>
          {allowed&&<button className="primary-button" disabled={busy===row.id}
            onClick={()=>void change('/api/finance/reviews/approve',row)}>
              {busy===row.id?'复核中…':'审核并入账'}</button>}
          {canCancel&&<button className="secondary-button" disabled={busy===row.id}
            onClick={()=>void change('/api/finance/reviews/cancel',row)}>撤回申请</button>}
        </article>
      })}
      {rows.length===0&&<div className="empty-state">
        暂无财务高风险审批记录。提交银行核销、调整分录或税务状态变更后会出现在这里。
      </div>}
    </div>
  </section>
}
