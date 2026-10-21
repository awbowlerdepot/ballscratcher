import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { getArticle, img, type ArticleDetail } from "../lib/api";
import { BigButton, Spinner, TakeItWithYou } from "../components/ui";

// Article reader (runbook 6eh): a Learn review or guide laid out for the
// big screen, with the ball's kiosk page and QR hand-off for ball reviews.
export default function LearnArticle() {
  const { slug = "" } = useParams();
  const [a, setA] = useState<(ArticleDetail & { product_id?: string | null }) | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    setA(null);
    setError(false);
    window.scrollTo(0, 0);
    getArticle(slug).then(setA).catch(() => setError(true));
  }, [slug]);

  if (error) return <p className="text-2xl text-mute">We couldn't open this article.</p>;
  if (!a) return <Spinner />;
  const hero = a.action_shot_image_url || a.product_shot_image_url;
  const list = (title: string, items?: string[] | null) =>
    items?.length ? (
      <section key={title}>
        <h2 className="mb-4 font-display text-3xl font-bold text-ink">{title}</h2>
        <ul className="list-disc space-y-3 pl-8 text-2xl leading-relaxed text-mute">
          {items.map((it, i) => (
            <li key={i}>{it}</li>
          ))}
        </ul>
      </section>
    ) : null;
  const para = (title: string, text?: string | null) =>
    text ? (
      <section key={title}>
        <h2 className="mb-4 font-display text-3xl font-bold text-ink">{title}</h2>
        {text.split(/\n\s*\n/).map((p, i) => (
          <p key={i} className="mb-4 text-2xl leading-relaxed text-mute">
            {p}
          </p>
        ))}
      </section>
    ) : null;

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-10">
      {hero && (
        <div className="overflow-hidden rounded-[2rem]">
          <img src={img(hero, 1600, 800, "cover") ?? hero} alt="" className="max-h-[520px] w-full object-cover" />
        </div>
      )}
      <div>
        <h1 className="font-display text-6xl font-bold leading-tight text-ink">{a.title}</h1>
        {a.hook && <p className="mt-5 text-3xl leading-snug text-mute">{a.hook}</p>}
      </div>
      {a.product_id && (
        <div className="flex flex-wrap items-center gap-6">
          <BigButton to={`/ball/${a.product_id}`}>See the ball: specs, chart & videos</BigButton>
        </div>
      )}
      {(a.sections ?? []).map((s) => para(s.heading, s.body))}
      {list("Key takeaways", a.key_takeaways)}
      {para("Performance", a.performance_summary)}
      <div className="grid grid-cols-2 gap-8">
        {list("Pros", a.pros)}
        {list("Cons", a.cons)}
      </div>
      <div className="grid grid-cols-2 gap-8">
        {list("Who it's for", a.who_should_buy)}
        {list("Who should skip it", a.who_should_skip)}
      </div>
      {para("Buying tips", a.buying_tips)}
      {para(a.kind === "video" ? "The bottom line" : "Verdict", a.verdict)}
      {a.faq?.length ? (
        <section>
          <h2 className="mb-4 font-display text-3xl font-bold text-ink">FAQ</h2>
          <div className="flex flex-col gap-5">
            {a.faq.map((f, i) => (
              <div key={i} className="rounded-3xl border-2 border-edge bg-panel p-6">
                <div className="font-display text-2xl font-bold text-ink">{f.question}</div>
                <p className="mt-2 text-xl text-mute">{f.answer}</p>
              </div>
            ))}
          </div>
        </section>
      ) : null}
      {a.product_id && <TakeItWithYou ballId={a.product_id} name={a.title.split(":")[0]} />}
    </div>
  );
}
