// FEATURE: 03_Admin_운영자 콘솔 화면

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ADMIN_OVERVIEW,
  BUFFER_STATUS,
  CURRENT_POLICY_ID,
  MANUAL_APPROVAL_ENABLED,
  POLICY_OPTIONS,
  REQUEST_LOG,
} from '@/routes/admin/mock'

export function AdminPage() {
  const navigate = useNavigate()
  const [isManualApproval, setIsManualApproval] = useState(MANUAL_APPROVAL_ENABLED)
  const [policyId, setPolicyId] = useState(CURRENT_POLICY_ID)

  const handleKillSwitch = () => {
    window.confirm('현재 송출 항목을 즉시 폴백 음악으로 교체할까요?')
  }

  return (
    <div>
      <aside>
        <div>onAIr</div>
        <nav>
          <button type="button" onClick={() => navigate('/')}>
            홈 · 방송 선택
          </button>
          <button type="button" onClick={() => navigate('/admin')}>
            운영자 콘솔
          </button>
        </nav>
      </aside>

      <main>
        <header>
          <h1>운영자 콘솔</h1>
          <span>LIVE · {ADMIN_OVERVIEW.liveLabel}</span>
          <span>
            {ADMIN_OVERVIEW.viewerCount}명 시청 중 · 대기 요청 {ADMIN_OVERVIEW.pendingRequestCount}건
          </span>
        </header>

        <section>
          <h2>킬 스위치</h2>
          <p>현재 송출 항목을 즉시 폴백 음악으로 교체합니다.</p>
          <button type="button" onClick={handleKillSwitch}>
            즉시 폴백 전환
          </button>
        </section>

        <section>
          <h2>수동 승인 모드</h2>
          <p>켜면 모든 대본이 방송 전 운영자 승인을 거칩니다.</p>
          <label>
            <input
              type="checkbox"
              checked={isManualApproval}
              onChange={(event) => setIsManualApproval(event.target.checked)}
            />
            {isManualApproval ? '켜짐 (수동 승인 중)' : '꺼짐 (자동 운영 중)'}
          </label>
        </section>

        <section>
          <h2>버퍼 상태 (D_total)</h2>
          <progress value={BUFFER_STATUS.dTotalSec} max={BUFFER_STATUS.thresholdSec * 2} />
          <p>
            D_total {BUFFER_STATUS.dTotalSec}초 · 임계값 {BUFFER_STATUS.thresholdSec}초 (
            {BUFFER_STATUS.state})
          </p>
        </section>

        <section>
          <h2>현재 편성 정책</h2>
          <div>
            {POLICY_OPTIONS.map((option) => (
              <button
                key={option.id}
                type="button"
                aria-pressed={policyId === option.id}
                onClick={() => setPolicyId(option.id)}
              >
                {option.label}
              </button>
            ))}
          </div>
          <p>실험/운영 목적으로 정책을 즉시 전환합니다.</p>
        </section>

        <section>
          <h2>최근 요청 로그</h2>
          <table>
            <thead>
              <tr>
                <th>시각</th>
                <th>요청</th>
                <th>상태</th>
                <th>정책</th>
              </tr>
            </thead>
            <tbody>
              {REQUEST_LOG.map((item) => (
                <tr key={item.id}>
                  <td>{item.time}</td>
                  <td>{item.request}</td>
                  <td>{item.status}</td>
                  <td>{item.policyLabel}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </main>
    </div>
  )
}
