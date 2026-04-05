---
name: jellyfin-media-advisor
description: Expert media recommendation strategies using Jellyfin library tools
version: 1.0.0
author: hermes
license: MIT
metadata:
  hermes:
    tags: [Jellyfin, Media, Movies, TV Shows, Recommendations]
    related_skills: []
    homepage: https://jellyfin.org
prerequisites:
  env_vars: [JELLYFIN_API_KEY]
---

# Jellyfin Media Advisor

Use the Jellyfin tools to help users explore, analyze, and get recommendations from their personal media library.

## Available Tools

- `jellyfin_search` -- Search by title, genre, year, type with sorting
- `jellyfin_library_stats` -- Library overview and genre breakdown
- `jellyfin_get_details` -- Full metadata for a specific item
- `jellyfin_similar` -- Find similar items (Jellyfin's similarity engine)
- `jellyfin_recent` -- Recently added media

## Strategy: Cross-Referencing External Lists

When the user asks "which IMDB top 250 movies do I have?":

1. You already know the IMDB Top 250 (or can web_search for the current list).
2. For each title, call `jellyfin_search` with the movie name and year.
3. Use `execute_code` to batch this efficiently -- build a Python script that calls the Jellyfin API directly using the env vars (`JELLYFIN_URL`, `JELLYFIN_API_KEY`), avoiding 250 LLM round-trips.
4. Present results as: found (with rating comparison), not found, and partial matches.

**Efficiency tip**: Search for distinctive single-word titles exactly. For common words, include the year to disambiguate.

## Strategy: Mood-Based Recommendations

When the user asks for a recommendation by mood or criteria:

1. Map the mood to genres:
   - "something fun" -> Comedy
   - "edge of my seat" -> Thriller, Action
   - "make me think" -> Drama, Mystery, Sci-Fi
   - "feel-good" -> Comedy, Romance, Animation
   - "scary" -> Horror, Thriller
2. Call `jellyfin_search` with the mapped genres, sorted by CommunityRating Descending.
3. Pick 3-5 top results and call `jellyfin_get_details` for rich descriptions.
4. Present each with: title, year, rating, a compelling 1-2 sentence pitch from the overview, and runtime.

## Strategy: "What Should I Watch Tonight?"

1. Call `jellyfin_library_stats` to understand the library.
2. Call `jellyfin_recent` to see newest additions (user might not have watched these).
3. Pick a genre the user hasn't explored recently, call `jellyfin_search` with SortBy=Random.
4. Provide 3 options across different genres with brief pitches.

## Strategy: Actor/Director Exploration

When the user mentions an actor or director:

1. Search for one known film by that person.
2. Get details to confirm the person is in the cast/crew.
3. The Jellyfin search API doesn't filter by person directly, so search broadly and use `jellyfin_get_details` on promising results to check cast lists.
4. Alternatively, use `jellyfin_similar` on a known film by that person.

## Response Format

When presenting movie recommendations, use this format:

- **Title** (Year) -- Rating/10
  Genre1, Genre2 | Runtime min
  > Brief compelling pitch from overview

When presenting library stats, highlight interesting facts:
- Total items, genre distribution, decade distribution
- Most represented genres, gaps in the collection
