import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { bootstrapSession } from "./api";
import { App } from "./App";
import { bootstrapEntry } from "./entry";
import { Loading } from "./ui";
import "./styles.css";
import "./brand.css";
const JourneyPreview = lazy(() =>
  import("./JourneyPreview").then((module) => ({
    default: module.JourneyPreview,
  })),
);
// Run once outside React effects: StrictMode must never exchange an invite twice.
// The explicit preview never mounts App, exchanges invitations or checks accounts.
const entry = bootstrapEntry(location.pathname, bootstrapSession);
if (entry.kind === "live") void entry.bootstrap.catch(() => {});
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {entry.kind === "preview" ? (
      <Suspense fallback={<Loading label="Opening the client journey…" />}>
        <JourneyPreview />
      </Suspense>
    ) : (
      <App bootstrap={entry.bootstrap} />
    )}
  </StrictMode>,
);
