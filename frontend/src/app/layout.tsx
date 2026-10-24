import React from "react";
import "./globals.css";

export const metadata = {
  title: "Aegis-CI Release Gate",
  description: "Zero-Rollback Multi-Agent CI/CD Release Gate",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="bg-slate-950 text-slate-100 antialiased">{children}</body>
    </html>
  );
}
