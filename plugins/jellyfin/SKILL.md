---
name: jellyfin-media-advisor
description: Expert media recommendation strategies using Jellyfin library tools
version: 2.0.0
author: hermes
license: MIT
metadata:
  hermes:
    tags: [Jellyfin, Media, Movies, TV Shows, Recommendations]
    related_skills: []
    homepage: https://jellyfin.org
prerequisites:
  env_vars: [JELLYFIN_API_KEY]  # OR JELLYFIN_USER + JELLYFIN_PASSWORD
---
# Jellyfin Media Advisor

Use the Jellyfin tools to help users explore, analyze, and get recommendations from their personal media library.

## Available Tools

### Search & Discovery
- `jellyfin_search` -- Search by title, genre, year, type, studio, content rating, runtime range, and person with sorting
- `jellyfin_library_stats` -- Library overview and genre breakdown
- `jellyfin_get_details` -- Full metadata for a specific item (includes IMDB/TMDB/TVDB IDs)
- `jellyfin_similar` -- Find similar items (Jellyfin's similarity engine)
- `jellyfin_recent` -- Recently added media
- `jellyfin_all_movies` -- Complete movie list with IMDB/TMDB/TVDB IDs

### Series Navigation
- `jellyfin_get_seasons(series_id)` -- List all seasons for a TV series
- `jellyfin_get_episodes(series_id, season_id?)` -- List episodes, optionally filtered by season
- `jellyfin_next_up` -- Next-up queue for series with recent viewing activity

### Collections
- `jellyfin_list_collections` -- Browse all collections (BoxSets)
- `jellyfin_collection_items(collection_id)` -- Items inside a specific collection

### Library Structure
- `jellyfin_list_views` -- Top-level library views (Movies, TV Shows, etc.)
- `jellyfin_browse_folder(parent_id)` -- Navigate into a specific library folder

### Reference Lists
- `jellyfin_genres` -- All genres in the library
- `jellyfin_studios` -- All studios in the library

## Authentication

Set one of the following in `~/.hermes/.env`:
1. `JELLYFIN_API_KEY` -- preferred, use Jellyfin Dashboard -> API Keys
2. `JELLYFIN_USER` + `JELLYFIN_PASSWORD` -- alternative, token is cached after first auth

The `JELLYFIN_URL` defaults to `http://localhost:8096`. Set `JELLYFIN_USER_ID` to skip auto-detection.

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

1. Use `jellyfin_search` with the `person` parameter to find all media featuring that person.
2. Get details on promising results with `jellyfin_get_details`.
3. Alternatively, use `jellyfin_similar` on a known film by that person.

## Strategy: TV Series Exploration

When the user asks about a TV show:

1. Search for the series with `jellyfin_search` (use `media_type="Series"`).
2. Use `jellyfin_get_seasons(series_id)` to show all seasons.
3. Use `jellyfin_get_episodes(series_id, season_id)` to drill into a specific season.
4. For each episode, show title, episode number, overview, and runtime.

## Strategy: Collection Exploration

When the user wants to browse collections:

1. Call `jellyfin_list_collections` to see all collections with item counts.
2. For any interesting collection, call `jellyfin_collection_items(collection_id)` to see what is inside.
3. Present the collection contents with brief descriptions.

## Strategy: Library Structure Navigation

When the user wants to explore the library structure:

1. Call `jellyfin_list_views` to see top-level sections (Movies, TV Shows, etc.).
2. Use `jellyfin_browse_folder(view_id)` to navigate into a section.
3. Further drill down with `jellyfin_browse_folder` on sub-folders.
4. Combine with `media_type` filter for focused browsing.

## Strategy: Studio-Based Discovery

When the user asks about a specific studio or wants to explore by studio:

1. Call `jellyfin_studios` to see all studios represented in the library.
2. Use `jellyfin_search` with the `studio` parameter to find all media from a specific studio.
3. Present results sorted by rating or year.

## Strategy: Runtime-Constrained Recommendations

When the user has limited time ("I only have 90 minutes"):

1. Use `jellyfin_search` with `max_runtime_minutes` to filter for movies that fit.
2. Sort by CommunityRating Descending for best options within the time constraint.
3. Present options with exact runtimes.

## Response Format

When presenting movie recommendations, use this format:

- **Title** (Year) -- Rating/10
  Genre1, Genre2 | Runtime min
  > Brief compelling pitch from overview

When presenting library stats, highlight interesting facts:
- Total items, genre distribution, decade distribution
- Most represented genres, gaps in the collection

When presenting TV series episodes, use this format:

- S01E01 - "Episode Title" (Runtime min)
  > Brief overview summary
