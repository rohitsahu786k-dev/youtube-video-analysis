import type { Metadata } from "next";import "./globals.css";import { Shell } from "@/components/shell";
export const metadata:Metadata={title:{default:"FX Trader Video Intelligence",template:"%s · FX Intelligence"},description:"Evidence-first personal Forex trading video research platform."};
export default function Layout({children}:{children:React.ReactNode}){return <html lang="en"><body><Shell>{children}</Shell></body></html>}
