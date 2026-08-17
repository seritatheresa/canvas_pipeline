import requests
import json
import os
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration
CANVAS_URL = os.getenv("CANVAS_URL", "https://your-canvas-instance.instructure.com")
API_TOKEN = os.getenv("CANVAS_TOKEN", os.getenv("CANVAS_API_TOKEN", "your-api-token-here"))
ACCOUNT_ID = os.getenv("CANVAS_ACCOUNT_ID", "your-account-id")

# Term ID filter - only update courses from this term
TERM_ID = os.getenv("TERM_ID", "202630")

# End date for courses (Friday 8/07/2026 at 5:00 PM UTC)
END_AT = "2026-08-07T17:00:00Z"

# Headers for API requests
HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json"
}

def read_courses_json(filepath, term_filter):
    """Read courses from Canvas JSON export, filtering by term."""
    courses = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            all_courses = json.load(f)
            
            # Filter courses by term_filter in sis_course_id
            for course in all_courses:
                sis_course_id = course.get('sis_course_id', '')
                if sis_course_id.startswith(term_filter):
                    courses.append({
                        'id': course.get('id'),
                        'name': course.get('name'),
                        'course_code': course.get('course_code'),
                        'sis_course_id': sis_course_id,
                        'workflow_state': course.get('workflow_state')
                    })
    except FileNotFoundError:
        print(f"Error: File not found: {filepath}")
        return []
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}")
        return []
    except Exception as e:
        print(f"Error reading JSON: {e}")
        return []
    
    return courses


def update_course(canvas_course_id):
    """Update a single course with the end_at date."""
    url = f"{CANVAS_URL}/api/v1/courses/{canvas_course_id}"
    
    payload = {
        "course[end_at]": END_AT,
        "course[restrict_enrollments_to_course_dates]": True
    }
    
    try:
        response = requests.put(url, headers=HEADERS, data=payload)
        
        # Log response details for debugging
        if response.status_code != 200:
            error_detail = f"Status: {response.status_code}, Response: {response.text}"
            return False, error_detail
        
        response.raise_for_status()
        return True, response.json()
    except requests.exceptions.RequestException as e:
        return False, f"Request failed: {str(e)}"
    except Exception as e:
        return False, f"Error: {str(e)}"


def batch_update_courses(course_ids, account_id):
    """Update multiple courses at once using batch endpoint."""
    url = f"{CANVAS_URL}/api/v1/accounts/{account_id}/courses"
    
    # Batch endpoint uses form data
    payload = {
        "event": "offer",  # Required parameter, but we can also update other fields
    }
    
    # Add course IDs
    for i, course_id in enumerate(course_ids):
        payload[f"course_ids[{i}]"] = course_id
    
    # Add end_at parameter
    payload["course[end_at]"] = END_AT
    payload["course[restrict_enrollments_to_course_dates]"] = True
    
    try:
        response = requests.put(url, headers=HEADERS, data=payload)
        response.raise_for_status()
        return True, response.json()
    except requests.exceptions.RequestException as e:
        return False, str(e)


def main():
    """Main function to update courses from canvas_courses JSON."""
    
    # Read courses from JSON
    json_path = Path(__file__).parent / "output" / "canvas_courses_202630.json"
    
    if not json_path.exists():
        print(f"Error: JSON file not found at {json_path}")
        return
    
    print(f"Reading courses from: {json_path}")
    courses = read_courses_json(json_path, TERM_ID)
    
    if not courses:
        print(f"No courses found for term: {TERM_ID}")
        return
    
    print(f"\n--- Updating courses for Term: {TERM_ID} ---")
    print(f"Found {len(courses)} courses to update")
    print(f"Setting end_at to: {END_AT}")
    print("-" * 80)
    
    # Configuration check
    if API_TOKEN == "your-api-token-here" or CANVAS_URL == "https://your-canvas-instance.instructure.com":
        print("\n⚠️  ERROR: Please configure the following environment variables:")
        print("   - CANVAS_URL: Your Canvas instance URL")
        print("   - CANVAS_API_TOKEN: Your Canvas API token")
        print("\nExample in .env file:")
        print("   CANVAS_URL=https://mvsu.instructure.com")
        print("   CANVAS_TOKEN=your-token-here")
        return
    
    # Update courses using numeric IDs from JSON
    print("\n--- Updating courses individually ---")
    success_count = 0
    failed_courses = []
    
    for i, course in enumerate(courses, 1):
        canvas_id = course['id']
        course_code = course['course_code']
        course_name = course['name']
        
        print(f"[{i}/{len(courses)}] {course_code}: {course_name} (ID: {canvas_id})...", end=" ")
        
        success, response = update_course(canvas_id)
        
        if success:
            print("✓")
            success_count += 1
        else:
            print(f"✗ {response}")
            failed_courses.append(f"{course_code}: {response}")
    
    print("-" * 80)
    print(f"Summary: {success_count}/{len(courses)} courses updated successfully")
    
    if failed_courses:
        print(f"\nFailed courses:")
        for failed in failed_courses:
            print(f"  - {failed}")


if __name__ == "__main__":
    main()
