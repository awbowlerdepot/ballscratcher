import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getArticles, getSignage, img, type ArticleCard, type SignageSpec } from "../lib/api";

// Attract loop (runbook 6eh): what the kiosk shows when nobody's using it,
// and where the idle reset returns. It cycles the newest ball reviews: a
// ball's approved in-store signage clip when it has one (runbook 6cn, a
// 9:16 loop, framed on the right of the landscape screen), otherwise the
// article's hero shot with a slow push-in. Touch anywhere to start.
const SLIDE_MS = 9000;
const MAX_SLIDES = 10;

interface Slide {
  card: ArticleCard;
  signage: SignageSpec | null;
}

const heroSrc = (c: ArticleCard) => img(c.product_shot_image_url || c.primary_image_url, 1600, 1000, "cover") ?? "";

export default function Attract() {
  const navigate = useNavigate();
  const [slides, setSlides] = useState<Slide[]>([]);
  const [i, setI] = useState(0);

  useEffect(() => {
    let cancelled = false;
    getArticles({ limit: 40 })
      .then(async (cards) => {
        const balls = cards.filter((c) => c.article_kind !== "video" && (c.product_shot_image_url || c.primary_image_url)).slice(0, MAX_SLIDES);
        const withSignage = await Promise.all(
          balls.map(async (card) => ({
            card,
            signage: card.slug ? await getSignage(card.slug).catch(() => null) : null,
          })),
        );
        // Warm every slide's image up front: the resizer makes a size the
        // first time it's asked for, which would otherwise show a blank slide.
        withSignage.forEach((sl) => {
          if (!sl.signage) new Image().src = heroSrc(sl.card);
        });
        if (!cancelled) setSlides(withSignage);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (slides.length < 2) return;
    const t = window.setInterval(() => setI((n) => (n + 1) % slides.length), SLIDE_MS);
    return () => window.clearInterval(t);
  }, [slides.length]);

  const slide = slides[i];
  return (
    <button
      type="button"
      onClick={() => navigate("/home")}
      className="relative flex h-screen w-screen cursor-default overflow-hidden bg-night text-left"
    >
      {slide && (
        <div key={slide.card.article_id} className="absolute inset-0 animate-[fadein_1.2s_ease-out]">
          {slide.signage ? (
            <div className="absolute inset-y-0 right-0 flex w-[45%] items-center justify-center bg-black">
              <video src={slide.signage.clip_url} autoPlay muted loop playsInline className="h-full w-auto object-cover" />
            </div>
          ) : (
            <img
              src={heroSrc(slide.card)}
              alt=""
              className="absolute inset-y-0 right-0 h-full w-[62%] animate-[pushin_9s_ease-out_forwards] object-cover"
            />
          )}
          <div className="absolute inset-0 bg-gradient-to-r from-night from-35% via-night/60 via-55% to-transparent" />
        </div>
      )}

      <div className="relative z-10 flex h-full w-[58%] flex-col justify-between px-20 py-16">
        <img
          src="/bowlerdepot-logo-white.png"
          alt="The Bowler Depot"
          className="h-24 w-auto self-start"
        />
        <div>
          {slide ? (
            <>
              <div className="mb-4 text-xl font-semibold uppercase tracking-[0.25em] text-brand">
                {slide.card.brand_name} · New review
              </div>
              <div className="font-display text-7xl font-bold leading-[1.05] text-ink">{slide.card.product_name}</div>
              {slide.signage?.tagline ? (
                <p className="mt-6 max-w-2xl text-3xl text-mute">{slide.signage.tagline}</p>
              ) : (
                <p className="mt-6 line-clamp-3 max-w-2xl text-2xl text-mute">{slide.card.hook}</p>
              )}
            </>
          ) : (
            <div className="font-display text-7xl font-bold leading-[1.05] text-ink">Find your next ball.</div>
          )}
        </div>
        <div className="flex items-center gap-5">
          <span className="relative flex h-6 w-6">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand opacity-60" />
            <span className="relative inline-flex h-6 w-6 rounded-full bg-brand" />
          </span>
          <span className="font-display text-4xl font-bold text-ink">Touch to explore</span>
          <span className="text-2xl text-mute">· find a ball, compare, watch reviews</span>
        </div>
      </div>
    </button>
  );
}
