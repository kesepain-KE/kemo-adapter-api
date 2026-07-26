import { KeyRound, ShieldCheck } from 'lucide-react'
import { Card, CardHeader, SectionTitle } from '../components/UI'

export default function Keys() {
  return <>
    <SectionTitle title="API 密钥管理" description="当前管理 API 尚未提供安全的密钥元数据和 CRUD 接口" eyebrow="Backend required"/>
    <div className="two-grid"><Card className="restart-card"><KeyRound/><h3>没有可查询的密钥数据</h3><p>后端只在认证时使用密钥，不会向浏览器返回 Token。创建、轮换、禁用和删除接口实现前，本页不会展示模拟密钥或提供无效按钮。</p></Card><Card><CardHeader title="安全边界" description="接入密钥 API 时必须保持" action={<ShieldCheck/>}/><div className="info-list"><div>完整 Token 从后端读回 <strong>禁止</strong></div><div>创建时单次展示 <strong>待实现</strong></div><div>Scope 与主体元数据 <strong>待实现</strong></div><div>热更新 api/keys.json <strong>核心已支持</strong></div></div></Card></div>
  </>
}
