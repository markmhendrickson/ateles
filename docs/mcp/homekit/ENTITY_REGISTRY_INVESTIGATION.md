# Entity Registry API Investigation

## Problem
The Home Assistant entity registry REST API endpoint `/api/config/entity_registry/{entity_id}` returns 404 Not Found when attempting to rename entities.

## Investigation Results

### Tested Endpoints (All Return 404)
- `GET /api/config/entity_registry` - 404
- `GET /api/config/entity_registry/light.techo_despacho` - 404
- `PUT /api/config/entity_registry/light.techo_despacho` - 404
- `GET /api/config/entity_registry/light/techo_despacho` - 404
- `GET /api/entity_registry` - 404
- `GET /api/entity_registry/light.techo_despacho` - 404

**Conclusion**: All entity registry REST API endpoints return 404, indicating the entity registry API is **not available via REST API**.

### Home Assistant Version
- Version: 2026.1.0
- Location: Home
- API is accessible (other endpoints work)

### Authentication
- Token is valid (other API calls work)
- Token has proper permissions

### Findings

1. **Entity Registry API Is WebSocket-Only**
   - **Confirmed**: All tested REST API endpoints for entity registry return 404
   - The entity registry API is **only available via WebSocket API**, not REST API
   - Home Assistant's REST API documentation doesn't document entity registry endpoints because they don't exist in REST
   - WebSocket API is required for entity registry operations (list, update, delete entities)

2. **Alternative Approaches**
   - **Manual UI**: Rename via Settings > Devices & Services > Entities > Edit
   - **WebSocket API**: Implement WebSocket client for entity registry operations
   - **Python Home Assistant Library**: Use `homeassistant` Python library which handles WebSocket internally

3. **Current Implementation Status**
   - The `rename_accessory` tool is implemented and will work if the REST API endpoint becomes available
   - Error handling provides clear instructions for manual renaming
   - Code tries multiple endpoint formats for compatibility

## Recommendations

1. **Short-term**: Use manual renaming via Home Assistant UI
2. **Long-term**: Implement WebSocket API support for entity registry operations
3. **Alternative**: Use Home Assistant Python library (`homeassistant` package) which abstracts WebSocket API

## References
- Home Assistant REST API: https://developers.home-assistant.io/docs/api/rest
- Home Assistant WebSocket API: https://developers.home-assistant.io/docs/api/websocket
- Entity Registry: https://developers.home-assistant.io/docs/entity_registry_index/
