import { useState } from "react";

export default function Newsletter() {
  const [email, setEmail] = useState("");
  const [submitted, setSubmitted] = useState(false);

  const handleSubmit = (e) => {
    e.preventDefault();
    if (email.trim()) {
      setSubmitted(true);
      setEmail("");
      setTimeout(() => setSubmitted(false), 3000);
    }
  };

  return (
    <section className="bg-bg-secondary border-y border-border">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-10 sm:py-14">
        <div className="max-w-2xl mx-auto text-center">
          <h2 className="font-serif text-2xl sm:text-3xl font-bold text-text-primary mb-2">
            Weekly Newsletter
          </h2>
          <p className="text-[14px] text-text-secondary mb-6 leading-relaxed">
            Get AI legal and technology intelligence delivered to your inbox every week.
          </p>

          <form onSubmit={handleSubmit} className="flex flex-col sm:flex-row gap-3 max-w-lg mx-auto">
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="Enter your email"
              className="flex-1 bg-white border border-border px-4 py-3 text-[14px] text-text-primary placeholder:text-text-muted outline-none transition-colors focus:border-accent rounded-lg"
              required
            />
            <button
              type="submit"
              className="px-6 py-3 bg-accent text-white text-[14px] font-bold uppercase tracking-wider hover:bg-accent-hover transition-colors shrink-0 rounded-lg"
            >
              {submitted ? "Subscribed!" : "Subscribe"}
            </button>
          </form>

          <p className="text-[11px] text-text-muted mt-3">
            No spam. Unsubscribe anytime.
          </p>
        </div>
      </div>
    </section>
  );
}
