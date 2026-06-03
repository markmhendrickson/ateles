# Asana API Plan Limitations

This document tracks Asana API features and properties that are unavailable or restricted based on plan tiers.

## Overview

The Asana MCP server is designed to work across all Asana plan tiers, but certain features may be limited or unavailable depending on your subscription level.

## Known Plan-Specific Limitations

### Custom Fields

**Premium/Business/Enterprise Only:**
- Custom field creation and modification
- Certain custom field types (e.g., formulas, dependencies)
- Custom field libraries

**Testing Approach:**
- Test custom field access on free tier
- Expect API errors for field creation
- Document specific error codes

**Error Handling:**
- Server gracefully handles missing custom field access
- Logs warning when custom fields are not accessible
- Continues operation without custom fields

### Advanced Search

**Business/Enterprise Only:**
- Advanced search queries
- Search by custom fields
- Complex filter combinations

**Testing Approach:**
- Test basic search on all tiers
- Test advanced search on premium tiers
- Document which search features fail

**Error Handling:**
- Falls back to basic search when advanced unavailable
- Returns appropriate error messages

### Portfolio Features

**Business/Enterprise Only:**
- Portfolio access and management
- Portfolio-level custom fields
- Portfolio status updates

**Testing Approach:**
- Attempt portfolio API calls
- Verify graceful failure on lower tiers
- Document error responses

**Error Handling:**
- Skips portfolio-related features
- Logs warning about unavailable features
- Continues with project-level operations

### Timeline Features

**Business/Enterprise Only:**
- Timeline view data
- Timeline-specific fields
- Dependency visualization

**Testing Approach:**
- Test timeline API access
- Verify fallback to basic task data
- Document missing fields

**Error Handling:**
- Omits timeline-specific data
- Uses standard task fields instead
- No disruption to core functionality

### Goals (Business/Enterprise Only)

**Business/Enterprise Only:**
- Goals API access
- Goal-task connections
- Goal progress tracking

**Testing Approach:**
- Test goals API on different tiers
- Document access restrictions
- Verify error handling

**Error Handling:**
- Skips goal-related features
- Continues with task operations

### Workflow Automation (Business/Enterprise Only)

**Business/Enterprise Only:**
- Rules and automation
- Trigger configuration
- Automated task actions

**Testing Approach:**
- Not tested (outside MCP server scope)
- Document as unavailable feature

**Error Handling:**
- Not implemented in MCP server

## Rate Limits

### Free Tier
- 150 requests per minute per user
- Lower burst limits

### Premium Tier
- 1,500 requests per minute per user
- Higher burst limits

### Business/Enterprise Tier
- Higher rate limits (documented in Asana API)
- Contact-based rate limit increases available

**Testing Approach:**
- Monitor rate limit headers in responses
- Test retry logic with rate limit errors
- Document rate limit handling

**Error Handling:**
- Implements exponential backoff
- Respects Retry-After headers
- Logs rate limit encounters

## Feature Availability Matrix

| Feature | Free | Premium | Business | Enterprise |
|---------|------|---------|----------|------------|
| Basic tasks | ✓ | ✓ | ✓ | ✓ |
| Projects | ✓ | ✓ | ✓ | ✓ |
| Sections | ✓ | ✓ | ✓ | ✓ |
| Tags | ✓ | ✓ | ✓ | ✓ |
| Comments | ✓ | ✓ | ✓ | ✓ |
| Attachments | ✓ | ✓ | ✓ | ✓ |
| Custom fields (read) | Limited | ✓ | ✓ | ✓ |
| Custom fields (write) | ✗ | ✓ | ✓ | ✓ |
| Advanced search | ✗ | Limited | ✓ | ✓ |
| Portfolios | ✗ | ✗ | ✓ | ✓ |
| Goals | ✗ | ✗ | ✓ | ✓ |
| Timeline | ✗ | ✗ | ✓ | ✓ |
| Workload | ✗ | ✗ | ✓ | ✓ |
| Rules/Automation | ✗ | ✗ | ✓ | ✓ |

## Testing Methodology

### 1. Feature Detection
- Attempt to access feature via API
- Capture error code and message
- Document availability

### 2. Graceful Degradation
- Implement fallback behavior
- Log warnings for unavailable features
- Continue core operations

### 3. Error Documentation
- Record specific error codes
- Document error messages
- Provide troubleshooting guidance

### 4. User Communication
- Clear error messages
- Guidance on plan upgrades
- Alternative approaches

## Common Error Codes

### 402 Payment Required
- Feature requires paid plan
- Upgrade needed to access

**Example:**
```json
{
  "errors": [{
    "message": "Custom fields are only available with premium"
  }]
}
```

### 403 Forbidden
- Insufficient permissions
- Feature not included in plan

**Example:**
```json
{
  "errors": [{
    "message": "This feature requires a Business or Enterprise plan"
  }]
}
```

### 404 Not Found
- Resource doesn't exist
- OR feature not available on plan

## Recommendations

1. **Start with Free Tier Testing**: Test all functionality on free tier first to establish baseline
2. **Document All Failures**: Record every feature that fails with plan limitations
3. **Implement Graceful Fallbacks**: Ensure server continues operating when features unavailable
4. **Provide Clear Messaging**: Users should understand why features are unavailable
5. **Regular Updates**: Asana updates plan features; review quarterly

## Updates Log

- **2025-12-30**: Initial documentation created
- **Future**: Will be updated as limitations are discovered through testing

## References

- [Asana API Documentation](https://developers.asana.com/docs)
- [Asana Pricing](https://asana.com/pricing)
- [Asana Plan Comparison](https://asana.com/guide/help/premium/about-premium)






