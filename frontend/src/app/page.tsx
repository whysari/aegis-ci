import ReleaseGateConsole from "../components/ReleaseGateConsole";

export const metadata = {
  title: "Aegis-CI | Zero-Rollback Multi-Agent PR Release Gate",
  description: "Deterministic 5-stage release gate for autonomous coding agents.",
};

export default function Home() {
  return <ReleaseGateConsole />;
}
