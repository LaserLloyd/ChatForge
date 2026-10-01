# Third-Party Notices

## Vendored Libraries

The following libraries are vendored in `src/aichat/web/static/vendor/`:

- **marked** 18.0.5 - MIT License
- **DOMPurify** 3.4.9 - Apache-2.0 OR MPL-2.0 License
- **highlight.js** 11.11.1 - BSD-3-Clause License
- **github-dark.css** (highlight.js themes) - BSD-3-Clause License

See `src/aichat/web/static/vendor/README.md` for full details.

## Code Adapted From

This project includes code adapted from the following repositories (all MIT License, owned by LaserLloyd):

- **StudioForge** (`src/studioforge/`) - Configuration, paths, logging, autostart, supervisor patterns
- **DisPatch_Chat** (`frontend/static/js/`) - Markdown rendering pipeline, chat UI patterns;
  (`backend/app/llm_api.py`) - the OpenAI and DeepSeek provider presets
- **CrucibleForge** (`crucibleforge/api.py`) - LLM streaming client and SSE handling

Each adapted function carries an "Adapted from" comment indicating the source.

## UI Theme

- **UnifyingTheme** (private repository) - UI design system copied verbatim
  - `src/aichat/web/static/ui-theme/` contains the complete theme bundle
  - Licensed under the owner's private licence; review required before public release

## Runtime Dependencies

The following are downloaded and used at runtime (not redistributed):

- **OpenVINO Model Server** 2026.4.0 - Apache-2.0 License
  - Downloaded from: https://github.com/openvinotoolkit/model_server/releases
  - Used for local NPU inference

- **Qwen Models** (OpenVINO-optimized versions) - Apache-2.0 License
  - Downloaded from: https://huggingface.co/OpenVINO/
  - Used for local inference on NPU

## Data Services

The tools call these public APIs at run time (no key; nothing is stored beyond a short cache):

- **Open-Meteo** (weather and geocoding) - data licensed CC BY 4.0; "Weather data by
  Open-Meteo.com" is credited in every weather result
- **Wikipedia** (page summaries) - text licensed CC BY-SA 4.0
- **Frankfurter** (exchange rates) - republishes the European Central Bank's reference rates
- **DuckDuckGo** via the `ddgs` package (web and news search)
