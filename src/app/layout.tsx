import type { Metadata } from "next";
import { DM_Sans } from "next/font/google";
import { Shell } from "@/components/shell";
import "./globals.css";

const dmSans=DM_Sans({subsets:["latin"],variable:"--font-dm-sans",display:"swap"});

export const metadata:Metadata={title:{default:"FX Trader Video Intelligence",template:"%s - FX Intelligence"},description:"Evidence-first personal Forex trading video research platform."};
export default function Layout({children}:{children:React.ReactNode}){return <html lang="en" className={dmSans.variable}><body><Shell>{children}</Shell></body></html>}
