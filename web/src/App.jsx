// Vantage: the home page at #/ and mission control at #/dashboard.
import { useEffect, useState } from "react";
import Console from "./Console.jsx";
import Home from "./Home.jsx";

const route = () => (window.location.hash.startsWith("#/dashboard") ? "dashboard" : "home");

export default function App() {
  const [page, setPage] = useState(route);

  useEffect(() => {
    const onHash = () => {
      const next = route();
      setPage((current) => {
        if (current !== next) window.scrollTo(0, 0);
        return next;
      });
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    document.title = page === "dashboard" ? "Vantage · Mission control" : "Vantage · Find people from above";
  }, [page]);

  return page === "dashboard" ? <Console /> : <Home />;
}
