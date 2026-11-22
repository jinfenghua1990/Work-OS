import {useEffect,useState} from 'react'

type CloseReview={
  id:string;period:string;status:'pending'|'approved'|'cancelled';
  requested_by:string;requested_at:string;approved_by:string|null;approved_at:string|null;
  cancelled_by:string|null;cancelled_at:string|null
}
type AuthState={named_accounts?:boolean;username?:string|null;role?:string|null}

async function api(path:string,method:'GET'|'POST'='GET',body?:unknown){
  const response=await fetch(path,{method,cache:'no-store',
    headers:method==='POST'?{'Content-Type':'application/json'}:undefined,
    body:method==='POST'?JSON.stringify(body):undefined})
  const result=await response.json()
  if(!response.ok)throw new Error(result.error||'财务审批请求失败')
  return result
}

export function PeriodCloseApprovals({refresh}:{refresh:()=>Promise<void>}){
  const [actor,setActor]=useState<AuthState|null>(null)
  const [requests,setRequests]=useState<CloseReview[]>([])
  const [busy,setBusy]=useState('')
  const [error,setError]=useState('')
  const [message,setMessage]=useState('')
  async function load(){
    try{
      const [session,items]=await Promise.all([
        api('/api/auth/status'),api('/api/periods/close/requests')
      ])
      setActor(session as AuthState)
      setRequests((items.requests||[]) as CloseReview[])
      setError('')
    }catch(exc){setError(exc instanceof Error?exc.message:'无法读取待审批月结')}
  }
  useEffect(()=>{void load()},[])
  async function act(path:string,request:CloseReview){
    setBusy(request.id);setError('');setMessage('')
    try{
      await api(path,'POST',{request_id:request.id})
      setMessage(request.period+(path.endsWith('/approve')
        ?' 已由当前审批账号复核并正式关账'
        :' 关账申请已撤回，原审核记录保留'))
      await Promise.all([load(),refresh()])
    }catch(exc){setError(exc instanceof Error?exc.message:'财务审批失败')}
    finally{setBusy('')}
  }
  const mayApprove=actor?.role==='owner'||actor?.role==='finance_approver'
  return <section className="panel finance-panel">
    <div className="panel-heading">
      <div><h2>月结独立审批</h2>
        <p>录入人在月结检查中提交申请，另一名具名审批员再次校验账务后锁账。所有审核和撤回记录保留。</p>
      </div>
      <button className="secondary-button" type="button" onClick={()=>void load()}>刷新审批</button>
    </div>
    {!actor?.named_accounts&&<p>当前为单人本地模式；如需双人财务审批，须先安全启用具名账号。</p>}
    {actor?.named_accounts&&<p>当前登录：{actor.username||'未知'}（{actor.role||'未知角色'}）</p>}
    {error&&<p className="form-error" role="alert">{error}</p>}
    {message&&<p role="status">{message}</p>}
    <div className="data-table">
      {requests.map(item=>{
        const awaiting=item.status==='pending'
        const selfApproval=actor?.username===item.requested_by
        const approvable=awaiting&&mayApprove&&!selfApproval
        const cancellable=awaiting&&(selfApproval||actor?.role==='owner')
        return <div className="data-row" key={item.id}>
          <span>
            <strong>{item.period} · {item.status==='approved'?'已关账':item.status==='pending'?'待复核':'已撤回'}</strong>
            <small>申请人 {item.requested_by} · {item.requested_at.slice(0,19).replace('T',' ')}</small>
          </span>
          <span>{item.status==='approved'
            ?'审批人 '+(item.approved_by||'—')
            :item.status==='cancelled'
            ?'撤回人 '+(item.cancelled_by||'—')
            :selfApproval?'等待其他账号审批':'待审批'}</span>
          {approvable&&<button className="primary-button" disabled={busy===item.id}
            onClick={()=>void act('/api/periods/close/approve',item)}>
            {busy===item.id?'复核中…':'复核并锁账'}
          </button>}
          {cancellable&&<button className="secondary-button" disabled={busy===item.id}
            onClick={()=>void act('/api/periods/close/cancel',item)}>撤回申请</button>}
        </div>
      })}
      {!requests.length&&<div className="empty-state">
        暂无月结审批。请先在经营报表中完成月结检查后发起申请。
      </div>}
    </div>
  </section>
}
