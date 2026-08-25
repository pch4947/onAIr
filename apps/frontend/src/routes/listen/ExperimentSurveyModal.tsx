// FEATURE: 02_Listen_사용자 실험 (세션 종료 설문 모달)

import { useState } from 'react'
import { SURVEY_QUESTIONS } from '@/routes/listen/mock'

const SCORES = [1, 2, 3, 4, 5]

interface ExperimentSurveyModalProps {
  onClose: () => void
}

export function ExperimentSurveyModal({ onClose }: ExperimentSurveyModalProps) {
  const [answers, setAnswers] = useState<Record<string, number>>({})

  const handleSelect = (questionId: string, score: number) => {
    setAnswers((prev) => ({ ...prev, [questionId]: score }))
  }

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    onClose()
  }

  return (
    <div role="dialog" aria-modal="true">
      <form onSubmit={handleSubmit}>
        <header>
          <h2>사용자 실험 참여</h2>
          <button type="button" onClick={onClose}>
            ×
          </button>
        </header>

        <h3>세션 종료 설문 ({SURVEY_QUESTIONS.length}문항)</h3>

        {SURVEY_QUESTIONS.map((question, index) => (
          <fieldset key={question.id}>
            <legend>
              {index + 1}. {question.label}
            </legend>
            {SCORES.map((score) => (
              <label key={score}>
                <input
                  type="radio"
                  name={question.id}
                  value={score}
                  checked={answers[question.id] === score}
                  onChange={() => handleSelect(question.id, score)}
                />
                {score}
              </label>
            ))}
          </fieldset>
        ))}

        <button type="submit">제출</button>
      </form>
    </div>
  )
}
