export default function Categories({ categories, current, onPick }) {
  return (
    <nav className="categories" aria-label="Categories">
      <ul>
        {categories.map((c) => (
          <li key={c.name}>
            <button type="button" className="category" aria-pressed={current === c.name} onClick={() => onPick(c.name)}>
              <span>{c.name}</span>
              <span className="category-count">{c.count}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
