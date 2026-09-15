// 02_Listen_사용자 실험

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
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-6">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-[560px] rounded-xl bg-surface shadow-lg"
      >
        <header className="flex items-center justify-between px-6 pb-4 pt-5">
          <p className="text-base font-semibold text-text">🧪 사용자 실험 참여</p>
          <button type="button" onClick={onClose} className="text-base text-text-muted">
            ×
          </button>
        </header>

        <div className="rounded-b-xl bg-bg p-6">
          <div className="flex flex-col gap-4 rounded-md border border-border bg-surface p-4">
            <p className="text-[15px] font-semibold text-text">
              세션 종료 설문 ({SURVEY_QUESTIONS.length}문항)
            </p>

            {SURVEY_QUESTIONS.map((question, index) => (
              <fieldset key={question.id} className="flex flex-col gap-1.5">
                <legend className="text-xs text-text">
                  {index + 1}. {question.label}
                </legend>
                <div className="flex gap-3">
                  {SCORES.map((score) => (
                    <label key={score} className="flex flex-col items-center gap-0.5">
                      <input
                        type="radio"
                        name={question.id}
                        value={score}
                        checked={answers[question.id] === score}
                        onChange={() => handleSelect(question.id, score)}
                        className="size-5 accent-primary"
                      />
                      <span className="text-[9px] text-text-muted">{score}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
            ))}

            <button
              type="submit"
              className="self-start rounded-md bg-primary px-5 py-2 text-[13px] font-semibold text-white"
            >
              제출
            </button>
          </div>
        </div>
      </form>
    </div>
  )
}
