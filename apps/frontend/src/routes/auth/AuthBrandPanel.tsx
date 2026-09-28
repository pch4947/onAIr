import { AUTH_FEATURES } from '@/routes/auth/mock'

export function AuthBrandPanel() {
  return (
    <aside className="flex w-[420px] shrink-0 flex-col gap-10 bg-sidebar px-10 py-12">
      <div className="flex items-center gap-2">
        <div className="size-7 rounded-md bg-primary-strong" />
        <p className="text-lg font-semibold text-white">onAIr</p>
      </div>

      <h1 className="text-[28px] font-bold leading-snug text-white">
        내 취향의 방송을 열고,
        <br />
        같은 취향인 사람들과 함께
      </h1>

      <ul className="flex flex-col gap-3">
        {AUTH_FEATURES.map((feature) => (
          <li key={feature.text} className="flex items-center gap-2 text-sm text-nav-inactive">
            <span>{feature.icon}</span>
            <span>{feature.text}</span>
          </li>
        ))}
      </ul>
    </aside>
  )
}
