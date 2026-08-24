import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Trader Video Intelligence",
  description: "Evidence-first Forex trading video research platform",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
