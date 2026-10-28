import { BrandWordmark } from '@/app/BrandWordmark'
import { AUTH_FEATURES } from '@/routes/auth/mock'

export function AuthBrandPanel() {
  return (
    <aside className="hidden w-120 shrink-0 flex-col gap-10 bg-sidebar px-10 py-12 md:flex">
      <div className="flex items-end">
        <img src="/logo_icon.png" alt="" className="size-30" />
        <BrandWordmark className="h-25 bg-white" />
      </div>

      <h1 className="text-[34px] font-bold leading-snug text-white">
        내 취향의 방송을 열고,
        <br />
        같은 취향인 사람들과 함께
      </h1>

      <ul className="flex flex-col gap-3">
        {AUTH_FEATURES.map((feature) => (
          <li key={feature.text} className="flex items-center gap-2 text-base text-nav-inactive">
            <span>{feature.icon}</span>
            <span>{feature.text}</span>
          </li>
        ))}
      </ul>
    </aside>
  )
}
