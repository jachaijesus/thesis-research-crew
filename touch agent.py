import json
import os
import re
import time
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

class ResearchPipeline:
    def __init__(self, topic: str):
        self.topic = topic
        # Grab the path from environment or default to local folder
        self.vault_path = os.getenv("OBSIDIAN_VAULT_PATH", "./ObsidianVault")
        self.raw_sources = []
        self.curated_sources = []

    def golem_source(self):
        """Gather and combine research papers from arXiv and Semantic Scholar."""
        ignored_terms = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "with"}
        search_terms = [
            term for term in re.findall(r"[a-z0-9]+", self.topic.lower())
            if term not in ignored_terms
        ]
        if not search_terms:
            raise ValueError("The project topic must contain searchable words.")

        self.raw_sources = []
        sources = []
        errors = []
        for source_method in (self._source_arxiv, self._source_semantic_scholar):
            try:
                sources.extend(source_method(search_terms))
            except RuntimeError as error:
                errors.append(str(error))
                print(f"⚠️ {error}")

        seen_titles = set()
        for source in sources:
            normalized_title = " ".join(source["Title"].casefold().split())
            if normalized_title not in seen_titles:
                self.raw_sources.append(source)
                seen_titles.add(normalized_title)

        if len(errors) == 2:
            self.raw_sources = []
            raise RuntimeError(
                "Golem could not retrieve results from either arXiv or Semantic Scholar. "
                + " | ".join(errors)
            )

        print(
            f"[Golem]: Combined {len(self.raw_sources)} unique papers from "
            f"arXiv and Semantic Scholar."
        )

    def _source_arxiv(self, search_terms):
        """Find up to five arXiv papers matching the project topic."""
        print(f"\n[Golem]: Querying arXiv for theme: '{self.topic}'...")
        search_query = " OR ".join(f"all:{term}" for term in search_terms)
        query_parameters = urllib.parse.urlencode({
            "search_query": search_query,
            "start": 0,
            "max_results": 5,
            "sortBy": "relevance",
            "sortOrder": "descending",
        })
        url = f"https://export.arxiv.org/api/query?{query_parameters}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "thesis-research-crew/1.0"},
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                root = ET.fromstring(response.read())
        except (urllib.error.URLError, TimeoutError, ET.ParseError) as error:
            raise RuntimeError(f"[Golem]: Failed to retrieve arXiv results: {error}") from error

        namespaces = {"atom": "http://www.w3.org/2005/Atom"}
        sources = []
        for entry in root.findall("atom:entry", namespaces):
            title = " ".join(entry.findtext("atom:title", "", namespaces).split())
            abstract = " ".join(entry.findtext("atom:summary", "", namespaces).split())
            link = entry.findtext("atom:id", "", namespaces).strip()
            published = entry.findtext("atom:published", "", namespaces).strip()
            if not title or not link or not published:
                raise RuntimeError(
                    "[Golem]: An arXiv result is missing its title, link, or publication date."
                )

            author_node = entry.find("atom:author/atom:name", namespaces)
            author = (
                " ".join(author_node.text.split())
                if author_node is not None and author_node.text
                else "Unknown Scholar"
            )
            abstract_excerpt = abstract[:140].rstrip()
            if len(abstract) > 140:
                abstract_excerpt += "..."

            sources.append({
                "Title": title,
                "Creator": author,
                "Medium": "Academic Paper (arXiv)",
                "Year": published[:4],
                "Link or location": link,
                "Why it's relevant": f"Abstract: {abstract_excerpt}",
                "Confidence": "Needs Check",
            })

        print(f"[Golem]: Retrieved {len(sources)} papers from arXiv.")
        return sources

    def _source_semantic_scholar(self, search_terms):
        """Find up to three papers from Semantic Scholar for the project topic."""
        print(f"\n[Golem]: Querying Semantic Scholar for theme: '{self.topic}'...")
        query_parameters = urllib.parse.urlencode({
            "query": " ".join(search_terms),
            "limit": 3,
            "fields": "title,authors,year,abstract,url,paperId",
        })
        request = urllib.request.Request(
            f"https://api.semanticscholar.org/graph/v1/paper/search?{query_parameters}",
            headers={
                "Accept": "application/json",
                "User-Agent": "thesis-research-crew/1.0",
            },
        )

        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt < 2:
                    retry_after = error.headers.get("Retry-After") if error.headers else None
                    try:
                        wait_time = min(max(float(retry_after), 0), 30)
                    except (TypeError, ValueError):
                        wait_time = 2 ** attempt
                    print(
                        f"⏳ [Golem]: Semantic Scholar rate limit reached. "
                        f"Retrying in {wait_time:g}s (attempt {attempt + 2}/3)..."
                    )
                    time.sleep(wait_time)
                    continue
                if error.code == 429:
                    raise RuntimeError(
                        "[Golem]: Semantic Scholar rate limit persisted after 3 attempts."
                    ) from error
                raise RuntimeError(
                    f"[Golem]: Semantic Scholar returned HTTP {error.code}: {error.reason}"
                ) from error
            except (
                urllib.error.URLError,
                TimeoutError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as error:
                raise RuntimeError(
                    f"[Golem]: Failed to retrieve Semantic Scholar results: {error}"
                ) from error

        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise RuntimeError(
                "[Golem]: Semantic Scholar returned an unexpected response structure."
            )

        sources = []
        for paper in data["data"]:
            if not isinstance(paper, dict):
                raise RuntimeError("[Golem]: Semantic Scholar returned an invalid paper record.")

            title = paper.get("title")
            if not isinstance(title, str) or not title.strip():
                continue

            authors = paper.get("authors") or []
            first_author = next(
                (
                    author["name"].strip()
                    for author in authors
                    if isinstance(author, dict)
                    and isinstance(author.get("name"), str)
                    and author["name"].strip()
                ),
                "Unknown Scholar",
            )
            paper_url = paper.get("url")
            if not isinstance(paper_url, str) or not paper_url.strip():
                paper_id = paper.get("paperId")
                paper_url = (
                    f"https://www.semanticscholar.org/paper/{paper_id}"
                    if isinstance(paper_id, str) and paper_id
                    else "Semantic Scholar"
                )

            abstract = paper.get("abstract")
            abstract = abstract if isinstance(abstract, str) else ""
            abstract_excerpt = abstract[:140].rstrip()
            if len(abstract) > 140:
                abstract_excerpt += "..."

            year = paper.get("year")
            sources.append({
                "Title": title.strip(),
                "Creator": first_author,
                "Medium": "Academic Paper (Semantic Scholar)",
                "Year": str(year) if year is not None else "Recent",
                "Link or location": paper_url,
                "Why it's relevant": f"Abstract: {abstract_excerpt}",
                "Confidence": "Needs Check",
            })

        print(f"[Golem]: Retrieved {len(sources)} papers from Semantic Scholar.")
        return sources

    def golem_checkpoint(self):
        """Review Golem's findings and select which assets pass to FaFo."""
        print(f"\n📋 [Golem Summary]: Sourcing sequence complete for '{self.topic}'.")
        print(f"Total entries discovered: {len(self.raw_sources)}")
        print("-" * 60)

        for idx, item in enumerate(self.raw_sources, 1):
            print(f" [{idx}] {item['Title']} ({item['Year']}) - Medium: {item['Medium']}")
            print(f"     Creator: {item['Creator']} | Confidence: {item['Confidence']}")
            print("-" * 60)

        print("Selection options:")
        print("  - Enter numbers separated by commas, spaces, or dashes (e.g., '1, 2' or '1-2')")
        print("  - Type 'all' to pass every finding to FaFo")
        print("  - Press [ENTER] without typing anything to skip all findings")

        user_input = input("\n👀 Select which findings to send to FaFo: ").strip().lower()

        if not user_input:
            print("\n[Checkpoint]: No items selected. Emptying manifest for FaFo.")
            self.raw_sources = []
            return

        if user_input == "all":
            print("\n[Checkpoint]: Passing all findings to FaFo.")
            return

        selected_sources = []
        seen_indices = set()
        selections = re.split(r"[\s,\-\u2013\u2014]+", user_input)

        for selection in selections:
            if not selection:
                continue
            if not selection.isdecimal():
                print(f"⚠️ Warning: Invalid selection '{selection}' was skipped.")
                continue

            index = int(selection)
            actual_idx = index - 1
            if not 0 <= actual_idx < len(self.raw_sources):
                print(f"⚠️ Warning: Selection [{index}] is out of bounds and was skipped.")
                continue
            if actual_idx in seen_indices:
                continue

            selected_sources.append(self.raw_sources[actual_idx])
            seen_indices.add(actual_idx)

        self.raw_sources = selected_sources
        print(f"\n✨ Manifest locked. Forwarding {len(self.raw_sources)} selected assets to FaFo.")

    def fafo_curate(self):
        """ FaFo (Curation Agent)
        Handles interactive validation loops, challenges inputs, and appends metadata.
        """
        print(f"\n[FaFo]: 'Fuck around and find out' loop engaged. Let's see what actually works.")

        for item in self.raw_sources:
            print("\n" + "="*60)
            for key, val in item.items():
                print(f"  {key}: {val}")
            print("="*60)

            if item["Confidence"] == "Needs Check":
                print("⚠️ [FaFo Warning]: Golem marked this asset as 'Needs Check'! Verify facts before keeping.")

            # Decision inputs matching FaFo requirements
            decision = ""
            while decision not in ["keep", "maybe", "cut"]:
                decision = input("-> Decision (Keep / Maybe / Cut): ").strip().lower()

            if decision == "cut":
                print(f"[FaFo]: Tossed out '{item['Title']}'. Moving on.")
                continue

            paper_target = input("-> What Paper/Chapter does this belong to?: ").strip()
            user_note = input("-> Write one line in your own words explaining why: ").strip()

            # FaFo challenges vague entries
            if len(user_note) < 15 or "good" in user_note or "test" in user_note:
                print(f"\n [FaFo Challenge]: '{user_note}' is weak. Are you sure you're engaging here?")
                user_note = input("-> Try again. Defend this choice in one sharp sentence: ").strip()

            # Compile extended metadata records
            curated_item = item.copy()
            curated_item["Decision"] = decision.capitalize()
            curated_item["Paper"] = paper_target if paper_target else "General Thesis Workspace"
            curated_item["My note"] = user_note

            self.curated_sources.append(curated_item)

    def chrysopoeia_archive(self):
        """ Chrysopoeia (Archiving Agent)
        Transmutes the curated arrays into perfectly structured local Obsidian markdown files.
        """
        print(f"\n[Chrysopoeia]: Commencing alchemical transmutative archiving sequence...")

        if not self.curated_sources:
            print("[Chrysopoeia]: No approved materials found to write. Sequence aborted.")
            return

        if not os.path.exists(self.vault_path):
            os.makedirs(self.vault_path)

        for asset in self.curated_sources:
            # Generate deterministic, clean local filenames
            safe_title = re.sub(r'[^\w\s-]', '', asset["Title"]).strip()
            safe_title = safe_title.replace(" ", "_")
            file_path = os.path.join(self.vault_path, f"{safe_title}.md")

            with open(file_path, "w", encoding="utf-8") as f:
                # Build Frontmatter metadata matching repository specification
                f.write("---\n")
                f.write(f"creator: \"{asset['Creator']}\"\n")
                f.write(f"medium: \"{asset['Medium']}\"\n")
                f.write(f"year: {asset['Year']}\n")
                f.write(f"confidence: \"{asset['Confidence']}\"\n")
                f.write(f"decision: \"{asset['Decision']}\"\n")
                f.write(f"target_paper: \"{asset['Paper']}\"\n")
                f.write(f"archived_date: {datetime.now().strftime('%Y-%m-%d')}\n")
                f.write("tags: [ecology, curatorial-practice, research-crew-output]\n")
                f.write("---\n\n")

                # Note Content
                f.write(f"# {asset['Title']}\n\n")
                f.write(f"### Sourcing Information\n")
                f.write(f"- **Location/Link:** {asset['Link or location']}\n")
                f.write(f"- **Golem Relevance Vector:** {asset['Why it\'s relevant']}\n\n")
                f.write(f"### Student Curatorial Analysis\n")
                f.write(f"> {asset['My note']}\n")

            print(f"[Chrysopoeia]: Transmuted asset into Note -> {file_path}")
        print("\n✨ All operations finalized successfully.")

if __name__ == "__main__":
    # Launch tracking pipeline
    crew_session = ResearchPipeline("Curatorial Practices intersecting with Regional Ecology")
    crew_session.golem_source()
    crew_session.golem_checkpoint()
    crew_session.fafo_curate()
    crew_session.chrysopoeia_archive()
