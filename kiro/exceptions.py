# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Exception handlers for Kiro Gateway.

Contains functions for handling validation errors and other exceptions
in a JSON-serialization compatible format.
"""

import json
from typing import Any, List, Dict

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger

MAX_VALIDATION_RESPONSE_BYTES: int = 8192
MAX_VALIDATION_ERRORS: int = 10
MAX_VALIDATION_FIELD_LENGTH: int = 120
MAX_VALIDATION_MESSAGE_LENGTH: int = 160
MAX_VALIDATION_LOCATION_DEPTH: int = 12
MAX_VALIDATION_ERROR_MESSAGE_LENGTH: int = 480


def summarize_validation_errors(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build bounded field diagnostics without echoing request inputs or context.

    Args:
        errors: Pydantic validation errors, which can reference the entire request.

    Returns:
        A bounded list containing only error types, field locations, and messages.
    """
    summary = []
    for error in errors[:MAX_VALIDATION_ERRORS]:
        location = error.get("loc", [])[:MAX_VALIDATION_LOCATION_DEPTH]
        item = {
            "type": str(error.get("type", "value_error"))[:MAX_VALIDATION_FIELD_LENGTH],
            "loc": [part if isinstance(part, int) else str(part)[:MAX_VALIDATION_FIELD_LENGTH]
                    for part in location],
            "msg": str(error.get("msg", "Invalid value"))[:MAX_VALIDATION_MESSAGE_LENGTH],
        }
        if len(json.dumps(summary + [item], ensure_ascii=False).encode("utf-8")) > MAX_VALIDATION_RESPONSE_BYTES // 2:
            break
        summary.append(item)
    return summary


def sanitize_validation_errors(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Converts validation errors to JSON-serializable format.
    
    Pydantic may include bytes objects in the 'input' field, which
    are not JSON-serializable. This function converts them to strings.
    
    Args:
        errors: List of validation errors from Pydantic
    
    Returns:
        List of errors with bytes converted to strings
    """
    sanitized = []
    for error in errors:
        sanitized_error = {}
        for key, value in error.items():
            if isinstance(value, bytes):
                # Convert bytes to string
                sanitized_error[key] = value.decode("utf-8", errors="replace")
            elif isinstance(value, (list, tuple)):
                # Recursively process lists
                sanitized_error[key] = [
                    v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
                    for v in value
                ]
            else:
                sanitized_error[key] = value
        sanitized.append(sanitized_error)
    return sanitized


def format_validation_message(errors: List[Dict[str, Any]]) -> str:
    """Expose bounded field diagnostics in clients that show only error.message.

    Args:
        errors: Sanitized field diagnostics without request inputs or context.

    Returns:
        A compact message containing up to three invalid field locations.
    """
    diagnostics = []
    for error in errors[:3]:
        field = ""
        for part in error["loc"]:
            if isinstance(part, int):
                field += f"[{part}]"
            else:
                field += ("." if field else "") + str(part)
        diagnostics.append(f"{field or 'request'}: {error['msg']}")
    message = "Request validation failed. " + (
        "; ".join(diagnostics) if diagnostics else "Check the request fields."
    )
    return message[:MAX_VALIDATION_ERROR_MESSAGE_LENGTH]


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    Pydantic validation error handler.
    
    Returns bounded field diagnostics without echoing conversation contents.
    Also flushes debug logs for validation errors when DEBUG_MODE is enabled.
    
    Args:
        request: FastAPI Request object
        exc: Validation exception from Pydantic
    
    Returns:
        JSONResponse with error details and status 422
    """
    # Union-validation failures can otherwise repeat a large conversation many times.
    # Never echo request bodies or Pydantic's input/ctx objects into client errors.
    errors = exc.errors()
    sanitized_errors = summarize_validation_errors(errors)
    
    logger.error(f"Validation error (422): {sanitized_errors}")
    
    # Flush debug logs for validation errors
    # This is called AFTER middleware has initialized debug logging,
    # so all app logs during request processing will be captured
    try:
        from kiro.debug_logger import debug_logger
        if debug_logger:
            error_message = f"Validation error: {sanitized_errors}"
            debug_logger.flush_on_error(422, error_message)
    except ImportError:
        pass  # debug_logger not available
    
    return JSONResponse(
        status_code=422,
        content={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": format_validation_message(sanitized_errors),
            },
            "detail": sanitized_errors,
            "omitted_errors": len(errors) - len(sanitized_errors),
            "body": "",
        },
    )
