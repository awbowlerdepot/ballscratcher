import { useEffect, useState, type ReactNode } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { LEARN_SITE, getPlotterBall, getProduct, img, titleCase, type LearnPlotterPoint, type ProductDetail } from "../lib/api";

// Phone hand-off page (runbook 6eh) -- what the kiosk's QR code opens on a
// customer's own phone. Al: "scan qr code that opens ball on a kiosk
// specific page that has links to the public sites learn,ecom,etc". A
// normal light mobile page outside the kiosk shell (no idle reset). Every
// outbound link is tagged utm_source=kiosk, utm_medium=qr and
// utm_campaign=<store location>, so bowlerdepot.com / Learn analytics can
// tell which kiosk sent the visit.
export default function Handoff() {
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const from = params.get("from") || "in-store";
  const [product, setProduct] = useState<ProductDetail | null>(null);
  const [ball, setBall] = useState<LearnPlotterPoint | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    document.title = "Your ball | BowlerDepot";
    getProduct(id).then(setProduct).catch(() => setError(true));
    getPlotterBall(id).then(setBall).catch(() => undefined);
  }, [id]);

  const tag = (url: string) => {
    try {
      const u = new URL(url);
      u.searchParams.set("utm_source", "kiosk");
      u.searchParams.set("utm_medium", "qr");
      u.searchParams.set("utm_campaign", from);
      return u.toString();
    } catch {
      return url;
    }
  };

  if (error) return <Page><p className="p-6 text-lg">This ball isn't available right now.</p></Page>;
  if (!product) return <Page><p className="p-6 text-lg text-[#6b6b63]">Loading…</p></Page>;

  const links: { label: string; sub: string; href: string | null; primary?: boolean }[] = [
    { label: "Shop on BowlerDepot.com", sub: "Order online or check availability", href: ball?.ecommerce_url ? tag(ball.ecommerce_url) : null, primary: true },
    { label: "Read the full review", sub: "Pros, cons and who it's for", href: ball?.article_slug ? tag(`${LEARN_SITE}/articles/${ball.article_slug}/`) : null },
    { label: "See it on the ball motion chart", sub: "Similar balls and what to try next", href: tag(`${LEARN_SITE}/plotter?ball=${product.id}`) },
    { label: "Manufacturer's page", sub: product.brand_name, href: product.url },
  ];
  const sku = [...(product.skus ?? [])].sort((a, b) => b.weight_lbs - a.weight_lbs)[0];

  return (
    <Page>
      <div className="bg-white px-5 pb-6 pt-5">
        <div className="mx-auto aspect-square w-56 overflow-hidden rounded-2xl">
          {product.primary_image_url && <img src={img(product.primary_image_url, 480, 480) ?? ""} alt={product.name} className="h-full w-full object-contain" />}
        </div>
        <div className="mt-4 text-center">
          <div className="text-sm font-semibold uppercase tracking-widest text-[#1f439e]">{product.brand_name}</div>
          <h1 className="font-display text-3xl font-bold text-[#0f0f2d]">{product.name}</h1>
          <p className="mt-1 text-base text-[#6b6b63]">
            {[product.coverstock_name, titleCase(product.coverstock_type), titleCase(product.core_type)].filter(Boolean).join(" · ")}
          </p>
          {sku?.rg != null && (
            <p className="mt-1 text-sm text-[#6b6b63]">
              {sku.weight_lbs} lb: RG {sku.rg.toFixed(2)} · Diff {sku.differential?.toFixed(3) ?? "--"}
              {sku.mass_bias != null ? ` · MB ${sku.mass_bias.toFixed(3)}` : ""}
            </p>
          )}
        </div>
      </div>
      <div className="flex flex-col gap-3 p-5">
        {links
          .filter((l) => l.href)
          .map((l) => (
            <a
              key={l.label}
              href={l.href!}
              className={`rounded-2xl px-5 py-4 ${l.primary ? "bg-[#1f439e] text-white" : "border border-[#e8e2d6] bg-white text-[#0f0f2d]"}`}
            >
              <div className="text-lg font-semibold">{l.label} ›</div>
              <div className={`text-sm ${l.primary ? "text-white/80" : "text-[#6b6b63]"}`}>{l.sub}</div>
            </a>
          ))}
        <p className="mt-4 text-center text-sm text-[#6b6b63]">Questions? Our pro shop staff are happy to help.</p>
      </div>
    </Page>
  );
}

function Page({ children }: { children: ReactNode }) {
  return (
    <div className="selectable min-h-screen bg-[#faf8f4] font-sans text-[#0f0f2d]">
      <div className="flex justify-center border-b border-[#e8e2d6] bg-white py-3">
        <img
          src="/bowlerdepot-logo-dark.png"
          alt="The Bowler Depot"
          className="h-9 w-auto"
        />
      </div>
      <div className="mx-auto max-w-md">{children}</div>
    </div>
  );
}
