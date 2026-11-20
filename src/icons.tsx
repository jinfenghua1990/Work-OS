import React from 'react'

type IconProps = { size?: number; strokeWidth?: number; className?: string }

function IconFrame({ children, size = 16, strokeWidth = 1.8, className }: IconProps & { children: React.ReactNode }) {
  return <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>
}

export const Activity = (p: IconProps) => <IconFrame {...p}><path d="M3 13h4l2-6 4 11 2-6h6"/><circle cx="3" cy="13" r="1" fill="currentColor" stroke="none"/></IconFrame>
export const ArrowDownLeft = (p: IconProps) => <IconFrame {...p}><path d="M19 5 5 19M5 8v11h11"/></IconFrame>
export const ArrowRight = (p: IconProps) => <IconFrame {...p}><path d="M4 12h15M13 5l7 7-7 7"/></IconFrame>
export const ArrowUpRight = (p: IconProps) => <IconFrame {...p}><path d="M5 19 19 5M8 5h11v11"/></IconFrame>
export const Bell = (p: IconProps) => <IconFrame {...p}><path d="M5 16h14l-2-3V9a5 5 0 0 0-10 0v4l-2 3Z"/><path d="M10 19a2 2 0 0 0 4 0"/></IconFrame>
export const Bike = (p: IconProps) => <IconFrame {...p}><circle cx="6" cy="16" r="4"/><circle cx="18" cy="16" r="4"/><path d="m6 16 4-8 4 8H6Zm4-8h4m-1 0 2 3h3"/></IconFrame>
export const BriefcaseBusiness = (p: IconProps) => <IconFrame {...p}><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5h8v2M3 12h18m-11 0v2h4v-2"/></IconFrame>
export const CalendarDays = (p: IconProps) => <IconFrame {...p}><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 10h18"/><path d="M8 14h.01M12 14h.01M16 14h.01M8 17h.01M12 17h.01"/></IconFrame>
export const Check = (p: IconProps) => <IconFrame {...p}><path d="m4 12 5 5L20 6"/></IconFrame>
export const ChevronDown = (p: IconProps) => <IconFrame {...p}><path d="m6 9 6 6 6-6"/></IconFrame>
export const CircleHelp = (p: IconProps) => <IconFrame {...p}><circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.6 2.6 0 1 1 4.4 1.9c-1.1 1-1.9 1.3-1.9 2.8M12 17h.01"/></IconFrame>
export const Coffee = (p: IconProps) => <IconFrame {...p}><path d="M5 9h12v6a4 4 0 0 1-4 4H9a4 4 0 0 1-4-4V9Zm12 2h2a2 2 0 1 1 0 4h-2M8 5c-1-1 1-2 0-3m4 3c-1-1 1-2 0-3"/></IconFrame>
export const Command = (p: IconProps) => <IconFrame {...p}><path d="M8 8V5a3 3 0 1 0-3 3h14a3 3 0 1 0-3-3v14a3 3 0 1 0 3-3H5a3 3 0 1 0 3 3V8Z"/></IconFrame>
export const FileCheck2 = (p: IconProps) => <IconFrame {...p}><path d="M6 3h8l4 4v14H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm8 0v5h5M8 14l2.5 2.5L16 11"/></IconFrame>
export const FileSpreadsheet = (p: IconProps) => <IconFrame {...p}><path d="M6 3h8l4 4v14H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm8 0v5h5M7 11h10M7 15h10M7 18h10M11 11v7"/></IconFrame>
export const Filter = (p: IconProps) => <IconFrame {...p}><path d="M4 5h16l-6 7v6l-4 2v-8L4 5Z"/></IconFrame>
export const LayoutDashboard = (p: IconProps) => <IconFrame {...p}><rect x="3" y="3" width="8" height="7" rx="1"/><rect x="14" y="3" width="7" height="4" rx="1"/><rect x="14" y="10" width="7" height="11" rx="1"/><rect x="3" y="13" width="8" height="8" rx="1"/></IconFrame>
export const MoreHorizontal = (p: IconProps) => <IconFrame {...p}><circle cx="5" cy="12" r="1" fill="currentColor"/><circle cx="12" cy="12" r="1" fill="currentColor"/><circle cx="19" cy="12" r="1" fill="currentColor"/></IconFrame>
export const Plus = (p: IconProps) => <IconFrame {...p}><path d="M12 5v14M5 12h14"/></IconFrame>
export const Search = (p: IconProps) => <IconFrame {...p}><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></IconFrame>
export const Settings2 = (p: IconProps) => <IconFrame {...p}><path d="M4 7h8m4 0h4M4 17h3m4 0h9"/><circle cx="14" cy="7" r="2"/><circle cx="9" cy="17" r="2"/></IconFrame>
export const ShieldCheck = (p: IconProps) => <IconFrame {...p}><path d="M12 3 20 6v5c0 5-3 8-8 10-5-2-8-5-8-10V6l8-3Z"/><path d="m8.5 12 2.3 2.3 4.7-5"/></IconFrame>
export const Sparkles = (p: IconProps) => <IconFrame {...p}><path d="m12 3 1.7 5.3L19 10l-5.3 1.7L12 17l-1.7-5.3L5 10l5.3-1.7L12 3Zm7 12 .8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15Z"/></IconFrame>
export const WalletCards = (p: IconProps) => <IconFrame {...p}><rect x="3" y="5" width="18" height="15" rx="2"/><path d="M3 9h18m-5 5h3m-11-9V3h11v2"/></IconFrame>
export const X = (p: IconProps) => <IconFrame {...p}><path d="m6 6 12 12M18 6 6 18"/></IconFrame>
