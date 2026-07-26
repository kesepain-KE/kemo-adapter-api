import { ChartNoAxesCombined, DatabaseZap } from 'lucide-react'
import { Card, CardHeader, SectionTitle } from '../components/UI'

export default function Logs() {
  return <>
    <SectionTitle title="统计与日志" description="当前后端没有持久化 Usage、请求追踪或脱敏日志查询接口" eyebrow="Backend required"/>
    <div className="two-grid"><Card className="restart-card"><ChartNoAxesCombined/><h3>观测数据尚未接入</h3><p>控制台不会生成虚假的调用量、Token、延迟、成功率或错误排行。接入真实存储和查询 API 后再启用统计视图。</p></Card><Card><CardHeader title="后端接入清单" description="所有数据必须可追溯" action={<DatabaseZap/>}/><div className="info-list"><div>请求与 Response 索引 <strong>待持久化</strong></div><div>Provider 精确 Usage <strong>待查询 API</strong></div><div>错误聚合与 retry_after <strong>待查询 API</strong></div><div>日志脱敏和租户隔离 <strong>必须实现</strong></div></div></Card></div>
  </>
}
