from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify, send_file
from app import db
from models import Course, Exam, Question, CourseOutcome, ProgramOutcome, Student, Score, ExamWeight, Log, CourseSettings, AchievementLevel, StudentExamAttendance
from datetime import datetime
import logging
import csv
import io
import os
from sqlalchemy import func
from routes.utility_routes import export_to_excel_csv
from decimal import Decimal
from flask import session
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import base64
import json
from flask import current_app

calculation_bp = Blueprint('calculation', __name__, url_prefix='/calculation')

# Helper function for achievement levels
def get_achievement_level(score, achievement_levels):
    """Get the achievement level for a given score"""
    for level in achievement_levels:
        if level.min_score <= score <= level.max_score:
            return {
                'name': level.name,
                'color': level.color
            }
    return {
        'name': 'Not Categorized',
        'color': 'secondary'
    }

@calculation_bp.route('/course/<int:course_id>')
def course_calculations(course_id):
    """Show calculation results for a course"""
    course = Course.query.get_or_404(course_id)
    
    # Get regular and makeup exams separately
    regular_exams = Exam.query.filter_by(course_id=course_id, is_makeup=False).order_by(Exam.created_at).all()
    makeup_exams = Exam.query.filter_by(course_id=course_id, is_makeup=True).order_by(Exam.created_at).all()
    
    # Create a map from original exam to makeup exam
    makeup_map = {}
    for makeup in makeup_exams:
        if makeup.makeup_for:
            makeup_map[makeup.makeup_for] = makeup
            
    # Combined list of all exams (for certain operations)
    all_exams = regular_exams + makeup_exams
    
    # Get list of mandatory exams
    mandatory_exams = [exam for exam in regular_exams if exam.is_mandatory]
    
    course_outcomes = CourseOutcome.query.filter_by(course_id=course_id).order_by(CourseOutcome.code).all()
    program_outcomes = set()
    for co in course_outcomes:
        for po in co.program_outcomes:
            program_outcomes.add(po)
    program_outcomes = sorted(list(program_outcomes), key=lambda po: po.code)
    
    # Get achievement levels for this course
    achievement_levels = AchievementLevel.query.filter_by(course_id=course_id).order_by(AchievementLevel.min_score.desc()).all()
    
    # Check if the course has achievement levels, if not, add default ones
    if not achievement_levels:
        # Add default achievement levels
        default_levels = [
            {"name": "Excellent", "min_score": 90.00, "max_score": 100.00, "color": "success"},
            {"name": "Better", "min_score": 70.00, "max_score": 89.99, "color": "info"},
            {"name": "Good", "min_score": 60.00, "max_score": 69.99, "color": "primary"},
            {"name": "Need Improvements", "min_score": 50.00, "max_score": 59.99, "color": "warning"},
            {"name": "Failure", "min_score": 0.01, "max_score": 49.99, "color": "danger"}
        ]
        
        for level_data in default_levels:
            level = AchievementLevel(
                course_id=course_id,
                name=level_data["name"],
                min_score=level_data["min_score"],
                max_score=level_data["max_score"],
                color=level_data["color"]
            )
            db.session.add(level)
        
        # Log action
        log = Log(action="ADD_DEFAULT_ACHIEVEMENT_LEVELS", 
                 description=f"Added default achievement levels to course: {course.code}")
        db.session.add(log)
        db.session.commit()
        
        # Refresh achievement levels
        achievement_levels = AchievementLevel.query.filter_by(course_id=course_id).order_by(AchievementLevel.min_score.desc()).all()
    
    # Get students from exam scores
    students = {}
    for exam in all_exams:
        for score in exam.scores:
            student_id = score.student_id
            if student_id not in students:
                students[student_id] = Student.query.get(student_id)

    # Check for necessary data
    has_course_outcomes = len(course_outcomes) > 0
    has_exam_questions = False
    for exam in all_exams:
        if len(exam.questions) > 0:
            has_exam_questions = True
            break
            
    has_student_scores = False
    for exam in all_exams:
        if len(exam.scores) > 0:
            has_student_scores = True
            break
            
    # Get exam weights and validate
    has_valid_weights = True
    total_weight = Decimal('0')
    exam_weights = {}
    
    # Get all weights for this course
    weights = ExamWeight.query.filter_by(course_id=course_id).all()
    for weight in weights:
        exam_weights[weight.exam_id] = weight.weight
    
    # Check all regular exams have weights
    for exam in regular_exams:
        if exam.id not in exam_weights:
            has_valid_weights = False
            break
        total_weight += exam_weights[exam.id]
    
    # Store the total weight percentage for display in template
    total_weight_percent = total_weight * Decimal('100')
    
    # Normalize weights if they don't add up to 1.0 (within small margin of error)
    normalized_weights = {}
    for exam_id, weight in exam_weights.items():
        if total_weight > Decimal('0'):
            normalized_weights[exam_id] = weight / total_weight
        else:
            normalized_weights[exam_id] = weight
    
    # We will still calculate results even if weights don't total exactly 100%
    # but we'll display a warning with the actual total
    
    # Calculate results if we have all the necessary data
    student_results = {}
    course_outcome_results = {}
    program_outcome_results = {}
    
    if has_course_outcomes and has_exam_questions and has_student_scores:
        # Create a map of exams to their questions
        questions_by_exam = {}
        for exam in all_exams:
            questions_by_exam[exam.id] = exam.questions
        
        # Create a map of course outcomes to their related questions
        outcome_questions = {}
        for co in course_outcomes:
            outcome_questions[co.id] = co.questions
        
        # Preload all scores for all exams and students
        scores_dict = {}
        student_ids = [s_id for s_id in students.keys()]
        exam_ids = [e.id for e in all_exams]
        
        # Query all scores for these students and exams
        if student_ids and exam_ids:
            all_scores = Score.query.filter(
                Score.student_id.in_(student_ids),
                Score.exam_id.in_(exam_ids)
            ).all()
            
            # Build a dictionary for O(1) lookups
            for score in all_scores:
                scores_dict[(score.student_id, score.question_id, score.exam_id)] = score.score
        
        # Preload attendance information
        attendance_dict = {}
        if student_ids and exam_ids:
            attendances = StudentExamAttendance.query.filter(
                StudentExamAttendance.student_id.in_(student_ids),
                StudentExamAttendance.exam_id.in_(exam_ids)
            ).all()
            
            for attendance in attendances:
                attendance_dict[(attendance.student_id, attendance.exam_id)] = attendance.attended
        
        # Calculate student achievement
        for student_id, student in students.items():
            # Initialize student data
            student_data = {
                'student_id': student.student_id,
                'name': f"{student.first_name} {student.last_name}".strip(),
                'course_outcomes': {},
                'exam_scores': {},
                'overall_percentage': 0,
                'skip': False
            }
            
            # Check if student should be excluded due to mandatory exam policy
            if mandatory_exams:
                skip_student = False
                for exam in mandatory_exams:
                    # Check if student attended the regular exam
                    regular_attended = attendance_dict.get((student_id, exam.id), True)
                    regular_score = None
                    if regular_attended:
                        regular_score = calculate_student_exam_score_optimized(
                            student_id, exam.id, scores_dict, questions_by_exam[exam.id], attendance_dict
                        )
                    
                    # Check if there's a makeup exam for this mandatory exam
                    makeup_exam = next((m for m in makeup_exams if m.makeup_for == exam.id), None)
                    makeup_attended = False
                    makeup_score = None
                    
                    if makeup_exam:
                        makeup_attended = attendance_dict.get((student_id, makeup_exam.id), False)
                        if makeup_attended:
                            makeup_score = calculate_student_exam_score_optimized(
                                student_id, makeup_exam.id, scores_dict, questions_by_exam[makeup_exam.id], attendance_dict
                            )
                    
                    # Skip student if they missed both the mandatory exam and its makeup (if exists)
                    # A student is excluded if:
                    # 1. They didn't attend the regular exam OR attended but got a None score (no data entered)
                    # AND
                    # 2. Either there's no makeup exam, or they didn't attend the makeup, or they attended but got a None score
                    if (not regular_attended or regular_score is None) and (not makeup_exam or not makeup_attended or makeup_score is None):
                        skip_student = True
                        break
                
                if skip_student:
                    student_data['skip'] = True
                    student_data['missing_mandatory'] = True
                    student_data['overall_percentage'] = 0
                    student_results[student_id] = student_data
                    continue
            
            # Calculate overall score as weighted average of exam scores
            total_weighted_score = Decimal('0')
            total_exams_weight = Decimal('0')
            
            # Create a map to track which exams have scores for this student
            student_exam_scores = {}
            for exam in all_exams:
                has_scores = False
                for question in questions_by_exam[exam.id]:
                    if (student_id, question.id, exam.id) in scores_dict:
                        has_scores = True
                        break
                if has_scores:
                    student_exam_scores[exam.id] = True
            
            # For each regular exam, check if student took the makeup instead
            for exam in regular_exams:
                # Get weight for this exam
                weight = normalized_weights.get(exam.id, Decimal('0'))
                if weight == Decimal('0'):
                    continue
                
                exam_score = None
                exam_to_use = exam
                
                # Check if there's a makeup for this exam
                if exam.id in makeup_map:
                    makeup_exam = makeup_map[exam.id]
                    # Check if student attended the makeup exam
                    makeup_attended = attendance_dict.get((student_id, makeup_exam.id), False)
                    
                    # If student attended the makeup exam, use that score exclusively
                    if makeup_attended:
                        makeup_score = calculate_student_exam_score_optimized(
                            student_id, makeup_exam.id, scores_dict, 
                            questions_by_exam[makeup_exam.id], attendance_dict
                        )
                        # Use makeup score even if it's 0 (as long as it's not None)
                        if makeup_score is not None:
                            exam_score = makeup_score
                            exam_to_use = makeup_exam
                            # Always use the original exam's weight for the makeup exam
                            student_data['exam_scores'][exam_to_use.name] = exam_score
                            total_weighted_score += exam_score * weight
                            total_exams_weight += weight
                            continue  # Skip to next exam, don't check the original
                
                # If no makeup was taken or makeup score is None, use the original exam
                if exam_score is None:
                    exam_score = calculate_student_exam_score_optimized(
                        student_id, exam.id, scores_dict,
                        questions_by_exam[exam.id], attendance_dict
                    )
                
                if exam_score is not None:
                    student_data['exam_scores'][exam_to_use.name] = exam_score
                    total_weighted_score += exam_score * weight
                    total_exams_weight += weight
            
            if total_exams_weight > Decimal('0'):
                student_data['overall_percentage'] = total_weighted_score / total_exams_weight
            
            # Calculate course outcome achievement
            for co in course_outcomes:
                co_score = Decimal('0')
                co_total_weight = Decimal('0')
                
                # For each regular exam, calculate outcome scores
                for exam in regular_exams:
                    weight = normalized_weights.get(exam.id, Decimal('0'))
                    if weight == Decimal('0'):
                        continue
                    
                    # Determine if we should use the makeup exam instead
                    use_makeup = False
                    actual_exam = exam
                    
                    if exam.id in makeup_map:
                        makeup_exam = makeup_map[exam.id]
                        makeup_attended = attendance_dict.get((student_id, makeup_exam.id), False)
                        
                        # If student attended the makeup, use that score exclusively
                        if makeup_attended:
                            use_makeup = True
                            actual_exam = makeup_exam
                    
                    # Skip this exam if student didn't attend and it's mandatory
                    if actual_exam.is_mandatory:
                        attended = attendance_dict.get((student_id, actual_exam.id), True)
                        if not attended:
                            continue
                    
                    exam_co_score = Decimal('0')
                    exam_co_total = Decimal('0')
                    
                    # Use the appropriate exam (original or makeup)
                    for question in questions_by_exam[actual_exam.id]:
                        # Check if this question is related to the course outcome
                        if co in question.course_outcomes:
                            # Get the student's score for this question
                            score_value = scores_dict.get((student_id, question.id, actual_exam.id))
                            if score_value is not None:
                                exam_co_score += Decimal(str(score_value))
                            exam_co_total += question.max_score
                    
                    if exam_co_total > Decimal('0'):
                        exam_co_percentage = (exam_co_score / exam_co_total) * Decimal('100')
                        co_score += exam_co_percentage * weight
                        co_total_weight += weight
                
                # Calculate the weighted average for this course outcome
                if co_total_weight > Decimal('0'):
                    student_data['course_outcomes'][co.code] = co_score / co_total_weight
                else:
                    student_data['course_outcomes'][co.code] = None
            
            student_results[student_id] = student_data
        
        # Calculate course outcome achievement levels
        for co in course_outcomes:
            co_total = Decimal('0')
            co_count = 0
            
            for student_id, student_data in student_results.items():
                if co.code in student_data['course_outcomes']:
                    co_total += student_data['course_outcomes'][co.code]
                    co_count += 1
            
            co_percentage = co_total / co_count if co_count > 0 else Decimal('0')
            achievement_level = get_achievement_level(float(co_percentage), achievement_levels)
            
            course_outcome_results[co.code] = {
                'description': co.description,
                'percentage': co_percentage,
                'achievement_level': achievement_level,
                'program_outcomes': [po.code for po in co.program_outcomes]
            }
        
        # Calculate program outcome achievement levels
        for po in program_outcomes:
            po_total = Decimal('0')
            po_count = 0
            contributing_cos = []
            
            for co in course_outcomes:
                if po in co.program_outcomes and co.code in course_outcome_results:
                    po_total += course_outcome_results[co.code]['percentage']
                    po_count += 1
                    contributing_cos.append(co.code)
            
            po_percentage = po_total / po_count if po_count > 0 else Decimal('0')
            achievement_level = get_achievement_level(float(po_percentage), achievement_levels)
            
            program_outcome_results[po.code] = {
                'description': po.description,
                'percentage': po_percentage,
                'achievement_level': achievement_level,
                'contributes': po_count > 0,
                'course_outcomes': contributing_cos
            }
    
    # Format student results for display
    formatted_student_results = {}
    for student_id, data in student_results.items():
        formatted_student_results[student_id] = data
    
    # Is this an AJAX request?
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    # Check if this is an AJAX request
    if is_ajax:
        # Return JSON data for AJAX requests
        return jsonify({
            'student_results': formatted_student_results,
            'course_outcome_results': course_outcome_results,
            'program_outcome_results': program_outcome_results,
            'exams': [{'id': e.id, 'name': e.name} for e in all_exams],
            'achievement_levels': [{'name': level.name, 'min_score': float(level.min_score), 
                                   'max_score': float(level.max_score), 'color': level.color} 
                                  for level in achievement_levels],
            'total_weight_percent': float(total_weight_percent)
        })
    
    return render_template('calculation/results.html', 
                         course=course,
                         exams=all_exams,
                         course_outcomes=course_outcomes,
                         program_outcomes=program_outcomes,
                         students=students,
                         student_results=formatted_student_results,
                         course_outcome_results=course_outcome_results,
                         program_outcome_results=program_outcome_results,
                         has_course_outcomes=has_course_outcomes,
                         has_exam_questions=has_exam_questions,
                         has_student_scores=has_student_scores,
                         has_valid_weights=has_valid_weights,
                         achievement_levels=achievement_levels,
                         total_weight_percent=total_weight_percent,
                         get_achievement_level=get_achievement_level,
                         active_page='courses')

@calculation_bp.route('/course/<int:course_id>/export')
def export_results(course_id):
    """Export calculation results to CSV in Excel-compatible format"""
    course = Course.query.get_or_404(course_id)
    
    # Get regular and makeup exams separately
    regular_exams = Exam.query.filter_by(course_id=course_id, is_makeup=False).order_by(Exam.created_at).all()
    makeup_exams = Exam.query.filter_by(course_id=course_id, is_makeup=True).order_by(Exam.created_at).all()
    
    # Create a map from original exam to makeup exam
    makeup_map = {}
    for makeup in makeup_exams:
        if makeup.makeup_for:
            makeup_map[makeup.makeup_for] = makeup
    
    # Combined list of all exams (for certain operations)
    all_exams = regular_exams + makeup_exams
    
    course_outcomes = CourseOutcome.query.filter_by(course_id=course_id).order_by(CourseOutcome.code).all()
    students = Student.query.filter_by(course_id=course_id).order_by(Student.student_id).all()
    
    # Get exam weights
    weights = {}
    for exam in regular_exams:
        weight = ExamWeight.query.filter_by(exam_id=exam.id).first()
        if weight:
            weights[exam.id] = weight.weight
        else:
            weights[exam.id] = Decimal('0')
    
    # Create a map of exams to their questions
    questions_by_exam = {}
    for exam in all_exams:
        questions_by_exam[exam.id] = exam.questions
    
    # Preload all scores
    scores_dict = {}
    student_ids = [s.id for s in students]
    exam_ids = [e.id for e in all_exams]
    
    if student_ids and exam_ids:
        scores = Score.query.filter(
            Score.student_id.in_(student_ids),
            Score.exam_id.in_(exam_ids)
        ).all()
        
        for score in scores:
            key = (score.student_id, score.question_id, score.exam_id)
            scores_dict[key] = score.score
    
    # Preload attendance information
    attendance_dict = {}
    if student_ids and exam_ids:
        attendances = StudentExamAttendance.query.filter(
            StudentExamAttendance.student_id.in_(student_ids),
            StudentExamAttendance.exam_id.in_(exam_ids)
        ).all()
        
        for attendance in attendances:
            attendance_dict[(attendance.student_id, attendance.exam_id)] = attendance.attended
    
    # Define CSV headers
    headers = ['Student ID', 'Student Name']
    
    # Add exam score headers
    for exam in regular_exams:
        headers.append(f'{exam.name} Score (%)')
    
    # Add course outcome headers
    for co in course_outcomes:
        headers.append(f'{co.code} Achievement (%)')
    
    # Add overall percentage header
    headers.append('Overall Weighted Score (%)')
    
    # Add student data rows
    data = []
    for student in students:
        # Create a map to track which exams have scores for this student
        student_exam_scores = {}
        for exam in all_exams:
            has_scores = False
            for question in questions_by_exam[exam.id]:
                if (student.id, question.id, exam.id) in scores_dict:
                    has_scores = True
                    break
            if has_scores:
                student_exam_scores[exam.id] = True
        
        # Use the headers list to ensure we maintain the correct order
        student_row = [""] * len(headers)
        
        # Set the student info
        student_row[0] = student.student_id
        student_row[1] = f"{student.first_name} {student.last_name}".strip()
        
        # Calculate weighted score
        weighted_score = Decimal('0')
        total_weight_used = Decimal('0')
        
        # Add exam scores
        exam_index_map = {}
        for i, header in enumerate(headers):
            for exam in regular_exams:
                if header == f'{exam.name} Score (%)':
                    exam_index_map[exam.id] = i
        
        for exam in regular_exams:
            idx = exam_index_map.get(exam.id)
            if idx is None:
                continue  # Skip if the exam isn't in headers
                
            # Check if this exam has a makeup and if the student took the makeup
            use_makeup = False
            actual_exam_id = exam.id
            
            if exam.id in makeup_map:
                makeup_exam = makeup_map[exam.id]
                if makeup_exam.id in student_exam_scores:
                    use_makeup = True
                    actual_exam_id = makeup_exam.id
            
            # Use the proper exam score (original or makeup)
            if use_makeup:
                exam_score = calculate_student_exam_score_optimized(student.id, actual_exam_id, scores_dict, questions_by_exam[actual_exam_id], attendance_dict)
                if exam_score is not None:
                    # Ensure consistent Decimal conversion
                    exam_score = Decimal(str(exam_score))
                    student_row[idx] = round(float(exam_score), 2)
                    
                    # Add to weighted score if weight exists
                    if exam.id in weights:
                        weighted_score += exam_score * weights[exam.id]
                        total_weight_used += weights[exam.id]
                    continue
            
            # Otherwise use regular exam score
            exam_score = calculate_student_exam_score_optimized(student.id, exam.id, scores_dict, questions_by_exam[exam.id], attendance_dict)
            if exam_score is not None:
                # Ensure consistent Decimal conversion
                exam_score = Decimal(str(exam_score))
                student_row[idx] = round(float(exam_score), 2)
                
                # Add to weighted score if weight exists
                if exam.id in weights:
                    weighted_score += exam_score * weights[exam.id]
                    total_weight_used += weights[exam.id]
        
        # Calculate and add course outcome scores
        co_index_map = {}
        for i, header in enumerate(headers):
            for co in course_outcomes:
                if header == f'{co.code} Achievement (%)':
                    co_index_map[co.id] = i
        
        for co in course_outcomes:
            idx = co_index_map.get(co.id)
            if idx is None:
                continue  # Skip if the outcome isn't in headers
            
            # Calculate course outcome achievement
            co_score = Decimal('0')
            co_total_weight = Decimal('0')
            
            # For each exam, calculate this outcome's score
            for exam in regular_exams:
                # Skip exams with no weight
                if exam.id not in weights or weights[exam.id] == Decimal('0'):
                    continue
                
                # Determine if we should use the makeup exam instead
                use_makeup = False
                actual_exam = exam
                if exam.id in makeup_map:
                    makeup_exam = makeup_map[exam.id]
                    if makeup_exam.id in student_exam_scores:
                        use_makeup = True
                        actual_exam = makeup_exam
                
                # Get the exam's weight
                weight = weights[exam.id]
                
                # Get questions for this exam
                questions = questions_by_exam[actual_exam.id]
                
                # Calculate total possible points for this outcome in this exam
                outcome_total_points = Decimal('0')
                outcome_scored_points = Decimal('0')
                
                for question in questions:
                    if co in question.course_outcomes:
                        outcome_total_points += question.max_score
                        score_value = scores_dict.get((student.id, question.id, actual_exam.id))
                        if score_value is not None:
                            outcome_scored_points += Decimal(str(score_value))
                
                if outcome_total_points > Decimal('0'):
                    outcome_percentage = (outcome_scored_points / outcome_total_points) * Decimal('100')
                    co_score += outcome_percentage * weight
                    co_total_weight += weight
            
            # Calculate final outcome percentage
            if co_total_weight > Decimal('0'):
                co_percentage = co_score / co_total_weight
                student_row[idx] = round(float(co_percentage), 2)
        
        # Calculate and add overall weighted score
        if total_weight_used > Decimal('0'):
            overall_percentage = weighted_score / total_weight_used
            student_row[-1] = round(float(overall_percentage), 2)
        
        # Add row to data
        data.append(student_row)
    
    # Log action
    log = Log(action="EXPORT_COURSE_RESULTS", 
             description=f"Exported calculation results for course: {course.code}")
    db.session.add(log)
    db.session.commit()
    
    # Convert data to CSV
    return export_to_csv(data, f"results_{course.code}", headers)

@calculation_bp.route('/all_courses', endpoint='all_courses')
def all_courses_calculations():
    """Show program outcome scores for all courses"""
    # Check if this is an AJAX request
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    # Get sort parameters from query string, default to course_code_asc
    sort_by = request.args.get('sort_by', 'course_code_asc')
    
    # Improved query with eager loading to reduce database hits
    courses = Course.query.options(
        db.joinedload(Course.course_outcomes),
        db.joinedload(Course.exams),
        db.joinedload(Course.students),
        db.joinedload(Course.settings)
    ).all()
    
    # Preload all program outcomes in one query
    program_outcomes = ProgramOutcome.query.all()
    program_outcomes_dict = {po.id: po for po in program_outcomes}
    
    # Get the display method from session or default to absolute
    display_method = session.get('display_method', 'absolute')
    
    # Initialize data structure to hold results for all courses
    all_results = {}
    
    # Extract years from courses for year filter
    years = sorted(set(course.semester.split(' ')[1] for course in courses 
                       if ' ' in course.semester), reverse=True)
    
    # Total number of courses for progress calculation
    total_courses = len([c for c in courses if c.course_outcomes and c.exams])
    processed_courses = 0
    
    # Prepare mapping tables to reduce lookups
    # Get all exam weights and score data in batches
    all_exam_weights = {}
    
    # Get all exam weights in a single query
    exam_weights = ExamWeight.query.all()
    for weight in exam_weights:
        all_exam_weights[(weight.exam_id)] = weight.weight
    
    # First, check if we need to update all course settings
    should_update_settings = False
    for course in courses:
        if course.settings and course.settings.success_rate_method != display_method:
            should_update_settings = True
            break

    # Update all course settings if needed
    if should_update_settings:
        for course in courses:
            if course.settings:
                course.settings.success_rate_method = display_method
        db.session.commit()

    for course in courses:
        # Skip courses with no outcomes or exams
        if not course.course_outcomes or not course.exams:
            continue
            
        # Update progress - useful for frontend progress tracking
        processed_courses += 1
        current_progress = int((processed_courses / total_courses) * 100) if total_courses > 0 else 100
        
        course_id = course.id
        course_code = course.code
        
        # Get the course settings or create default - use the preloaded relation
        settings = course.settings
        if not settings:
            settings = CourseSettings(
                course_id=course_id,
                success_rate_method=display_method,
                relative_success_threshold=60.0
            )
            db.session.add(settings)
            db.session.commit()
        
        # Update the calculation method based on session if needed
        if settings.success_rate_method != display_method:
            settings.success_rate_method = display_method
            db.session.commit()
        
        # Filter preloaded exams instead of new queries
        exams = [e for e in course.exams if not e.is_makeup]
        makeup_exams = [e for e in course.exams if e.is_makeup]
        
        # Get students for this course - use preloaded relation
        students = course.students
        
        # Check if we have all necessary data to calculate results
        if not exams or not students:
            continue
            
        # Get mandatory exams - filter in-memory
        mandatory_exams = [exam for exam in exams if exam.is_mandatory]
        
        # Get exam weights - use preloaded weights dict
        weights = {}
        total_weight = Decimal('0')
        for exam in exams:
            weight = all_exam_weights.get(exam.id)
            if weight is not None:
                weights[exam.id] = weight
                total_weight += weight
            else:
                weights[exam.id] = Decimal('0')
        
        # Normalize weights if they don't add up to 1.0
        normalized_weights = {}
        for exam_id, weight in weights.items():
            if total_weight > Decimal('0'):
                normalized_weights[exam_id] = weight / total_weight
            else:
                normalized_weights[exam_id] = weight
        
        # Preload all course outcomes for this course
        course_outcomes = course.course_outcomes
        
        # Preload all question-course outcome associations for this course
        question_ids = []
        for exam in exams:
            question_ids.extend([q.id for q in exam.questions])
        
        # Create a map of course outcomes to their related questions
        outcome_questions = {}
        for co in course_outcomes:
            outcome_questions[co.id] = co.questions
        
        # Create a map of exams to their questions
        questions_by_exam = {}
        for exam in exams + makeup_exams:
            questions_by_exam[exam.id] = exam.questions
        
        # Create a map of program outcomes to their related course outcomes
        program_to_course_outcomes = {}
        for po in program_outcomes:
            related_cos = [co for co in course_outcomes if po in co.program_outcomes]
            program_to_course_outcomes[po.id] = related_cos
        
        # Calculate student results based on selected method
        student_results = {}
        success_count = 0
        total_students = 0
        
        # Preload scores for this course to reduce database hits
        # Get all scores for this course's students in one query
        student_ids = [s.id for s in students]
        exam_ids = [e.id for e in exams + makeup_exams]
        
        # Only load scores if we have students and exams
        if student_ids and exam_ids:
            # Preload scores in batches if there are many students/exams
            batch_size = 100
            scores_dict = {}
            
            for i in range(0, len(student_ids), batch_size):
                batch_student_ids = student_ids[i:i+batch_size]
                scores = Score.query.filter(
                    Score.student_id.in_(batch_student_ids),
                    Score.exam_id.in_(exam_ids)
                ).all()
                
                for score in scores:
                    key = (score.student_id, score.question_id, score.exam_id)
                    scores_dict[key] = score.score
            
            # Preload attendance information
            attendance_dict = {}
            for i in range(0, len(student_ids), batch_size):
                batch_student_ids = student_ids[i:i+batch_size]
                attendances = StudentExamAttendance.query.filter(
                    StudentExamAttendance.student_id.in_(batch_student_ids),
                    StudentExamAttendance.exam_id.in_(exam_ids)
                ).all()
                
                for attendance in attendances:
                    attendance_dict[(attendance.student_id, attendance.exam_id)] = attendance.attended
        
        for student in students:
            # Initialize student results
            student_results[student.id] = {
                'student': student,
                'exam_scores': {},  # Raw scores per exam
                'weighted_score': Decimal('0'),  # Final weighted score
                'program_outcome_scores': {},  # Scores per program outcome
                'skip': False
            }
            
            # Check if student should be skipped due to mandatory exam policy
            if mandatory_exams:
                skip_student = False
                for exam in mandatory_exams:
                    # Check if student attended the regular exam
                    regular_attended = attendance_dict.get((student.id, exam.id), True)
                    regular_score = None
                    if regular_attended:
                        regular_score = calculate_student_exam_score_optimized(
                            student.id, exam.id, scores_dict, questions_by_exam[exam.id], attendance_dict
                        )
                    
                    # Check if there's a makeup exam for this mandatory exam
                    makeup_exam = next((m for m in makeup_exams if m.makeup_for == exam.id), None)
                    makeup_attended = False
                    makeup_score = None
                    
                    if makeup_exam:
                        makeup_attended = attendance_dict.get((student.id, makeup_exam.id), False)
                        if makeup_attended:
                            makeup_score = calculate_student_exam_score_optimized(
                                student.id, makeup_exam.id, scores_dict, questions_by_exam[makeup_exam.id], attendance_dict
                            )
                    
                    # Skip student if they missed both the mandatory exam and its makeup (if exists)
                    # A student is excluded if:
                    # 1. They didn't attend the regular exam OR attended but got a None score (no data entered)
                    # AND
                    # 2. Either there's no makeup exam, or they didn't attend the makeup, or they attended but got a None score
                    if (not regular_attended or regular_score is None) and (not makeup_exam or not makeup_attended or makeup_score is None):
                        skip_student = True
                        break
                
                if skip_student:
                    student_results[student.id]['skip'] = True
                    student_results[student.id]['missing_mandatory'] = True
                    student_results[student.id]['overall_percentage'] = 0
                    student_results[student.id] = student_results[student.id]
                    continue
            
            # Calculate exam scores for student
            for exam in exams:
                # Check if there's a makeup for this exam
                makeup_exam = next((m for m in makeup_exams if m.makeup_for == exam.id), None)
                
                # If there's a makeup exam, check if student attended it
                if makeup_exam:
                    makeup_attended = attendance_dict.get((student.id, makeup_exam.id), False)
                    
                    # If student attended the makeup, use that score exclusively
                    if makeup_attended:
                        makeup_score = calculate_student_exam_score_optimized(
                            student.id, makeup_exam.id, scores_dict, 
                            questions_by_exam[makeup_exam.id], attendance_dict
                        )
                        # Use makeup score even if it's 0 (as long as it's not None)
                        if makeup_score is not None:
                            student_results[student.id]['exam_scores'][exam.id] = makeup_score
                            continue  # Skip the original exam completely
                
                # If no makeup was attended or makeup score is None, use regular exam score
                exam_score = calculate_student_exam_score_optimized(
                    student.id, exam.id, scores_dict,
                    questions_by_exam[exam.id], attendance_dict
                )
                if exam_score is not None:
                    student_results[student.id]['exam_scores'][exam.id] = exam_score
            
            # Calculate weighted score
            weighted_score = Decimal('0')
            for exam_id, score in student_results[student.id]['exam_scores'].items():
                weighted_score += score * normalized_weights.get(exam_id, Decimal('0'))
            
            student_results[student.id]['weighted_score'] = weighted_score
            
            # Calculate program outcome scores
            for outcome in program_outcomes:
                score = calculate_program_outcome_score_optimized(student.id, outcome.id, course_id, scores_dict, program_to_course_outcomes, outcome_questions)
                student_results[student.id]['program_outcome_scores'][outcome.id] = score
                
            # Count successes for relative method
            total_students += 1
            if weighted_score >= settings.relative_success_threshold:
                success_count += 1
        
        # Calculate success rate based on method
        class_results = {
            'program_outcome_scores': {},
            'total_students': total_students,
            'success_count': success_count,
            'success_rate': (success_count / total_students * 100) if total_students > 0 else 0
        }
        
        # Calculate average program outcome scores
        for outcome in program_outcomes:
            # Only include valid students in the scores calculation - those who haven't been marked to skip
            scores = [r['program_outcome_scores'][outcome.id] for r in student_results.values() 
                    if not r.get('skip') and r['program_outcome_scores'][outcome.id] is not None]
            
            if scores:
                # For absolute method: average of all student scores
                if settings.success_rate_method == 'absolute':
                    avg_score = sum(scores) / len(scores)
                    class_results['program_outcome_scores'][outcome.id] = avg_score
                # For relative method: percentage of students who achieved the threshold
                else:  # 'relative'
                    success_students = sum(1 for score in scores if score >= settings.relative_success_threshold)
                    success_rate = (success_students / len(scores) * 100) if len(scores) > 0 else 0
                    class_results['program_outcome_scores'][outcome.id] = success_rate
            else:
                class_results['program_outcome_scores'][outcome.id] = None
        
        # Format program outcome results for template
        program_outcome_results = {}
        for outcome in program_outcomes:
            avg_score = class_results['program_outcome_scores'].get(outcome.id)
            
            # Check if this program outcome is linked to any course outcomes for this course
            contributes = False
            for co in course.course_outcomes:
                if outcome in co.program_outcomes:
                    contributes = True
                    break
                    
            program_outcome_results[outcome.code] = {
                'description': outcome.description,
                'percentage': avg_score if avg_score is not None else 0,
                'contributes': contributes
            }
        
        # Store results for this course
        all_results[course_code] = {
            'course': course,
            'program_outcome_results': program_outcome_results,
            'settings': settings,
            'avg_outcome_score': calculate_avg_outcome_score(program_outcome_results)
        }
    
    # Sort the results according to the sort parameter
    sorted_results = {}
    
    if sort_by == 'course_code_asc':
        sorted_keys = sorted(all_results.keys())
    elif sort_by == 'course_code_desc':
        sorted_keys = sorted(all_results.keys(), reverse=True)
    elif sort_by == 'course_name_asc':
        sorted_keys = sorted(all_results.keys(), key=lambda k: all_results[k]['course'].name.lower())
    elif sort_by == 'course_name_desc':
        sorted_keys = sorted(all_results.keys(), key=lambda k: all_results[k]['course'].name.lower(), reverse=True)
    elif sort_by == 'avg_score_asc':
        sorted_keys = sorted(all_results.keys(), key=lambda k: all_results[k]['avg_outcome_score'])
    elif sort_by == 'avg_score_desc':
        sorted_keys = sorted(all_results.keys(), key=lambda k: all_results[k]['avg_outcome_score'], reverse=True)
    else:
        # Default to course code ascending
        sorted_keys = sorted(all_results.keys())
    
    for key in sorted_keys:
        sorted_results[key] = all_results[key]
    
    # Log action
    log = Log(action="ALL_COURSES_CALCULATIONS", 
            description=f"Viewed program outcome scores for all courses")
    db.session.add(log)
    db.session.commit()
    
    # Check if this is an AJAX request
    if is_ajax:
        # Return JSON data for AJAX requests
        return jsonify({
            'all_results': {k: {
                'course': {
                    'id': v['course'].id,
                    'code': v['course'].code,
                    'name': v['course'].name,
                    'semester': v['course'].semester
                },
                'program_outcome_results': v['program_outcome_results'],
                'settings': {
                    'success_rate_method': v['settings'].success_rate_method,
                    'relative_success_threshold': float(v['settings'].relative_success_threshold),
                    'excluded': v['settings'].excluded
                },
                'avg_outcome_score': float(v['avg_outcome_score'])
            } for k, v in sorted_results.items()},
            'program_outcomes': [{'id': po.id, 'code': po.code, 'description': po.description} for po in program_outcomes],
            'progress': 100,  # Always 100 when returning final results
            'current_sort': sort_by
        })
    
    # For regular requests, render the template
    return render_template('calculation/all_courses.html', 
                          all_results=sorted_results,
                          program_outcomes=program_outcomes,
                          years=years,
                          active_page='all_courses',
                          current_sort=sort_by)

# Helper function to calculate average outcome score
def calculate_avg_outcome_score(program_outcome_results):
    """Calculate the average program outcome score for a course"""
    valid_scores = [float(po_data['percentage']) for po_code, po_data in program_outcome_results.items() 
                   if po_data['contributes'] and po_data['percentage'] is not None]
    
    if valid_scores:
        return sum(valid_scores) / len(valid_scores)
    else:
        return 0.0

@calculation_bp.route('/all_courses_loading', endpoint='all_courses_loading')
def all_courses_loading():
    """Redirects to all_courses for backward compatibility"""
    return redirect(url_for('calculation.all_courses'))

@calculation_bp.route('/all_courses/export', endpoint='export_all_courses')
def export_all_courses():
    """Export program outcome scores for all courses to CSV"""
    # Get sort parameters from query string, default to course_code_asc
    sort_by = request.args.get('sort_by', 'course_code_asc')
    
    # Use the same calculation logic as all_courses_calculations to ensure consistency
    # Improved query with eager loading to reduce database hits
    courses = Course.query.options(
        db.joinedload(Course.course_outcomes),
        db.joinedload(Course.exams),
        db.joinedload(Course.students),
        db.joinedload(Course.settings)
    ).all()
    
    # Preload all program outcomes in one query
    program_outcomes = ProgramOutcome.query.all()
    program_outcomes_dict = {po.id: po for po in program_outcomes}
    
    # Get the display method from session or default to absolute
    display_method = session.get('display_method', 'absolute')
    
    # Initialize data structure to hold results for all courses
    all_results = {}
    
    # Prepare mapping tables to reduce lookups
    # Get all exam weights and score data in batches
    all_exam_weights = {}
    
    # Get all exam weights in a single query
    exam_weights = ExamWeight.query.all()
    for weight in exam_weights:
        all_exam_weights[(weight.exam_id)] = weight.weight
    
    for course in courses:
        # Skip courses with no outcomes or exams
        if not course.course_outcomes or not course.exams:
            continue
        
        course_id = course.id
        course_code = course.code
        
        # Get the course settings or create default - use the preloaded relation
        settings = course.settings
        if not settings:
            settings = CourseSettings(
                course_id=course_id,
                success_rate_method=display_method,
                relative_success_threshold=60.0
            )
            db.session.add(settings)
            db.session.commit()
        
        # Skip excluded courses
        if settings.excluded:
            continue
            
        # Update the calculation method based on session if needed
        settings.success_rate_method = display_method
        
        # Filter preloaded exams instead of new queries
        exams = [e for e in course.exams if not e.is_makeup]
        makeup_exams = [e for e in course.exams if e.is_makeup]
        
        # Get students for this course - use preloaded relation
        students = course.students
        
        # Check if we have all necessary data to calculate results
        if not exams or not students:
            continue
            
        # Get mandatory exams - filter in-memory
        mandatory_exams = [exam for exam in exams if exam.is_mandatory]
        
        # Get exam weights - use preloaded weights dict
        weights = {}
        total_weight = Decimal('0')
        for exam in exams:
            weight = all_exam_weights.get(exam.id)
            if weight is not None:
                weights[exam.id] = weight
                total_weight += weight
            else:
                weights[exam.id] = Decimal('0')
        
        # Normalize weights if they don't add up to 1.0
        normalized_weights = {}
        for exam_id, weight in weights.items():
            if total_weight > Decimal('0'):
                normalized_weights[exam_id] = weight / total_weight
            else:
                normalized_weights[exam_id] = weight
        
        # Preload all course outcomes for this course
        course_outcomes = course.course_outcomes
        
        # Preload all question-course outcome associations for this course
        question_ids = []
        for exam in exams:
            question_ids.extend([q.id for q in exam.questions])
        
        # Create a map of course outcomes to their related questions
        outcome_questions = {}
        for co in course_outcomes:
            outcome_questions[co.id] = co.questions
        
        # Create a map of exams to their questions
        questions_by_exam = {}
        for exam in exams + makeup_exams:
            questions_by_exam[exam.id] = exam.questions
        
        # Create a map of program outcomes to their related course outcomes
        program_to_course_outcomes = {}
        for po in program_outcomes:
            related_cos = [co for co in course_outcomes if po in co.program_outcomes]
            program_to_course_outcomes[po.id] = related_cos
        
        # Calculate student results based on selected method
        student_results = {}
        success_count = 0
        total_students = 0
        
        # Preload scores for this course to reduce database hits
        # Get all scores for this course's students in one query
        student_ids = [s.id for s in students]
        exam_ids = [e.id for e in exams + makeup_exams]
        
        # Only load scores if we have students and exams
        if student_ids and exam_ids:
            # Preload scores in batches if there are many students/exams
            batch_size = 100
            scores_dict = {}
            
            for i in range(0, len(student_ids), batch_size):
                batch_student_ids = student_ids[i:i+batch_size]
                scores = Score.query.filter(
                    Score.student_id.in_(batch_student_ids),
                    Score.exam_id.in_(exam_ids)
                ).all()
                
                for score in scores:
                    key = (score.student_id, score.question_id, score.exam_id)
                    scores_dict[key] = score.score
            
            # Preload attendance information
            attendance_dict = {}
            for i in range(0, len(student_ids), batch_size):
                batch_student_ids = student_ids[i:i+batch_size]
                attendances = StudentExamAttendance.query.filter(
                    StudentExamAttendance.student_id.in_(batch_student_ids),
                    StudentExamAttendance.exam_id.in_(exam_ids)
                ).all()
                
                for attendance in attendances:
                    attendance_dict[(attendance.student_id, attendance.exam_id)] = attendance.attended
        
        for student in students:
            # Initialize student results
            student_results[student.id] = {
                'student': student,
                'exam_scores': {},  # Raw scores per exam
                'weighted_score': Decimal('0'),  # Final weighted score
                'program_outcome_scores': {},  # Scores per program outcome
                'skip': False
            }
            
            # Check if student should be skipped due to mandatory exam policy
            if mandatory_exams:
                skip_student = False
                for exam in mandatory_exams:
                    # Check if student attended the regular exam
                    regular_attended = attendance_dict.get((student.id, exam.id), True)
                    regular_score = None
                    if regular_attended:
                        regular_score = calculate_student_exam_score_optimized(
                            student.id, exam.id, scores_dict, questions_by_exam[exam.id], attendance_dict
                        )
                    
                    # Check if there's a makeup exam for this mandatory exam
                    makeup_exam = next((m for m in makeup_exams if m.makeup_for == exam.id), None)
                    makeup_attended = False
                    makeup_score = None
                    
                    if makeup_exam:
                        makeup_attended = attendance_dict.get((student.id, makeup_exam.id), False)
                        if makeup_attended:
                            makeup_score = calculate_student_exam_score_optimized(
                                student.id, makeup_exam.id, scores_dict, questions_by_exam[makeup_exam.id], attendance_dict
                            )
                    
                    # Skip student if they missed both the mandatory exam and its makeup (if exists)
                    # A student is excluded if:
                    # 1. They didn't attend the regular exam OR attended but got a None score (no data entered)
                    # AND
                    # 2. Either there's no makeup exam, or they didn't attend the makeup, or they attended but got a None score
                    if (not regular_attended or regular_score is None) and (not makeup_exam or not makeup_attended or makeup_score is None):
                        skip_student = True
                        break
                
                if skip_student:
                    student_results[student.id]['skip'] = True
                    student_results[student.id]['missing_mandatory'] = True
                    student_results[student.id]['overall_percentage'] = 0
                    student_results[student.id] = student_results[student.id]
                    continue
            
            # Calculate exam scores for student
            for exam in exams:
                # Check if there's a makeup for this exam
                makeup_exam = next((m for m in makeup_exams if m.makeup_for == exam.id), None)
                
                # If there's a makeup exam, check if student attended it
                if makeup_exam:
                    makeup_attended = attendance_dict.get((student.id, makeup_exam.id), False)
                    
                    # If student attended the makeup, use that score exclusively
                    if makeup_attended:
                        makeup_score = calculate_student_exam_score_optimized(
                            student.id, makeup_exam.id, scores_dict, 
                            questions_by_exam[makeup_exam.id], attendance_dict
                        )
                        # Use makeup score even if it's 0 (as long as it's not None)
                        if makeup_score is not None:
                            student_results[student.id]['exam_scores'][exam.id] = makeup_score
                            continue  # Skip the original exam completely
                
                # If no makeup was attended or makeup score is None, use regular exam score
                exam_score = calculate_student_exam_score_optimized(
                    student.id, exam.id, scores_dict,
                    questions_by_exam[exam.id], attendance_dict
                )
                if exam_score is not None:
                    student_results[student.id]['exam_scores'][exam.id] = exam_score
            
            # Calculate weighted score
            weighted_score = Decimal('0')
            for exam_id, score in student_results[student.id]['exam_scores'].items():
                weighted_score += score * normalized_weights.get(exam_id, Decimal('0'))
            
            student_results[student.id]['weighted_score'] = weighted_score
            
            # Calculate program outcome scores
            for outcome in program_outcomes:
                score = calculate_program_outcome_score_optimized(student.id, outcome.id, course_id, scores_dict, program_to_course_outcomes, outcome_questions)
                student_results[student.id]['program_outcome_scores'][outcome.id] = score
                
            # Count successes for relative method
            total_students += 1
            if weighted_score >= settings.relative_success_threshold:
                success_count += 1
        
        # Calculate success rate based on method
        class_results = {
            'program_outcome_scores': {},
            'total_students': total_students,
            'success_count': success_count,
            'success_rate': (success_count / total_students * 100) if total_students > 0 else 0
        }
        
        # Calculate average program outcome scores
        for outcome in program_outcomes:
            # Only include valid students in the scores calculation - those who haven't been marked to skip
            scores = [r['program_outcome_scores'][outcome.id] for r in student_results.values() 
                    if not r.get('skip') and r['program_outcome_scores'][outcome.id] is not None]
            
            if scores:
                # For absolute method: average of all student scores
                if settings.success_rate_method == 'absolute':
                    avg_score = sum(scores) / len(scores)
                    class_results['program_outcome_scores'][outcome.id] = avg_score
                # For relative method: percentage of students who achieved the threshold
                else:  # 'relative'
                    success_students = sum(1 for score in scores if score >= settings.relative_success_threshold)
                    success_rate = (success_students / len(scores) * 100) if len(scores) > 0 else 0
                    class_results['program_outcome_scores'][outcome.id] = success_rate
            else:
                class_results['program_outcome_scores'][outcome.id] = None
        
        # Format program outcome results for template
        program_outcome_results = {}
        for outcome in program_outcomes:
            avg_score = class_results['program_outcome_scores'].get(outcome.id)
            
            # Check if this program outcome is linked to any course outcomes for this course
            contributes = False
            for co in course.course_outcomes:
                if outcome in co.program_outcomes:
                    contributes = True
                    break
                    
            program_outcome_results[outcome.code] = {
                'description': outcome.description,
                'percentage': avg_score if avg_score is not None else 0,
                'contributes': contributes
            }
        
        # Store results for this course
        all_results[course_code] = {
            'course': course,
            'program_outcome_results': program_outcome_results,
            'settings': settings,
            'avg_outcome_score': calculate_avg_outcome_score(program_outcome_results)
        }
    
    # Sort the results according to the sort parameter
    sorted_results = {}
    
    if sort_by == 'course_code_asc':
        sorted_keys = sorted(all_results.keys())
    elif sort_by == 'course_code_desc':
        sorted_keys = sorted(all_results.keys(), reverse=True)
    elif sort_by == 'course_name_asc':
        sorted_keys = sorted(all_results.keys(), key=lambda k: all_results[k]['course'].name.lower())
    elif sort_by == 'course_name_desc':
        sorted_keys = sorted(all_results.keys(), key=lambda k: all_results[k]['course'].name.lower(), reverse=True)
    elif sort_by == 'avg_score_asc':
        sorted_keys = sorted(all_results.keys(), key=lambda k: all_results[k]['avg_outcome_score'])
    elif sort_by == 'avg_score_desc':
        sorted_keys = sorted(all_results.keys(), key=lambda k: all_results[k]['avg_outcome_score'], reverse=True)
    else:
        # Default to course code ascending
        sorted_keys = sorted(all_results.keys())
    
    for key in sorted_keys:
        sorted_results[key] = all_results[key]
    
    # Prepare data for export
    data = []
    
    # Create headers
    headers = ['Course Code', 'Course Name', 'Semester', 'Success Rate Method', 'Success Threshold']
    
    # Add program outcome headers in the same order as they appear on the page
    for po in program_outcomes:
        headers.append(f'PO: {po.code} (%)')
    
    # Add course data rows in the same format as shown on the page
    for course_code, course_data in sorted_results.items():
        course_row = {
            'Course Code': course_code,
            'Course Name': course_data['course'].name,
            'Semester': course_data['course'].semester,
            'Success Rate Method': course_data['settings'].success_rate_method,
            'Success Threshold': float(course_data['settings'].relative_success_threshold),
        }
        
        # Add program outcome values directly from the calculated results
        for po in program_outcomes:
            po_header = f'PO: {po.code} (%)'
            po_result = course_data['program_outcome_results'].get(po.code, {})
            
            if po_result.get('contributes', False) and po_result.get('percentage') is not None:
                course_row[po_header] = round(float(po_result['percentage']), 1)
            else:
                course_row[po_header] = 'N/A'
                
        data.append(course_row)
    
    # Add a summary row with program outcome averages
    summary_row = {
        'Course Code': 'AVERAGE',
        'Course Name': 'Program Outcome Averages',
        'Semester': '',
        'Success Rate Method': '',
        'Success Threshold': '',
    }
    
    # Calculate averages for each program outcome - using the same values displayed on the page
    for po in program_outcomes:
        po_header = f'PO: {po.code} (%)'
        valid_scores = []
        
        for course_code, course_data in sorted_results.items():
            po_result = course_data['program_outcome_results'].get(po.code, {})
            if po_result.get('contributes', False) and po_result.get('percentage') is not None:
                valid_scores.append(float(po_result['percentage']))
        
        if valid_scores:
            summary_row[po_header] = round(sum(valid_scores) / len(valid_scores), 1)
        else:
            summary_row[po_header] = 'N/A'
    
    data.append(summary_row)
    
    # Log action
    log = Log(action="EXPORT_ALL_COURSES", 
             description=f"Exported program outcome scores for all courses")
    db.session.add(log)
    db.session.commit()
    
    # Export data using utility function
    return export_to_excel_csv(data, "all_courses_program_outcomes", headers)

@calculation_bp.route('/course/<int:course_id>/settings', methods=['POST'])
def update_course_settings(course_id):
    """Update course calculation settings"""
    course = Course.query.get_or_404(course_id)
    
    # Get the settings or create new if it doesn't exist
    settings = CourseSettings.query.filter_by(course_id=course_id).first()
    if not settings:
        settings = CourseSettings(course_id=course_id)
        db.session.add(settings)
    
    # Update settings
    settings.success_rate_method = request.form.get('success_rate_method', 'absolute')
    try:
        settings.relative_success_threshold = Decimal(request.form.get('relative_success_threshold', '60.0'))
    except:
        settings.relative_success_threshold = Decimal('60.0')
    
    # Update excluded status
    settings.excluded = 'excluded' in request.form
    
    db.session.commit()
    
    # Log action
    log = Log(action="UPDATE_COURSE_SETTINGS",
             description=f"Updated calculation settings for course: {course.code}")
    db.session.add(log)
    db.session.commit()
    
    flash('Course calculation settings updated successfully.', 'success')
    
    # Return to the appropriate page
    referer = request.referrer
    if referer and 'all_courses' in referer:
        return redirect(url_for('calculation.all_courses'))
    else:
        return redirect(url_for('calculation.course_calculations', course_id=course_id))

@calculation_bp.route('/exam/<int:exam_id>/toggle_mandatory', methods=['POST'])
def toggle_mandatory_exam(exam_id):
    """Toggle the mandatory status of an exam"""
    exam = Exam.query.get_or_404(exam_id)
    course = Course.query.get_or_404(exam.course_id)
    
    # Toggle the status
    exam.is_mandatory = not exam.is_mandatory
    db.session.commit()
    
    # Log action
    status = "mandatory" if exam.is_mandatory else "optional"
    log = Log(action="TOGGLE_MANDATORY_EXAM",
             description=f"Changed exam '{exam.name}' in course '{course.code}' to {status}")
    db.session.add(log)
    db.session.commit()
    
    flash(f"Exam '{exam.name}' is now {status}.", 'success')
    return redirect(url_for('exam.manage_exams', course_id=course.id))

@calculation_bp.route('/course/<int:course_id>/toggle_exclusion', methods=['POST'])
def toggle_course_exclusion(course_id):
    """Toggle the exclusion status of a course"""
    course = Course.query.get_or_404(course_id)
    
    # Get the settings or create new if it doesn't exist
    settings = CourseSettings.query.filter_by(course_id=course_id).first()
    if not settings:
        settings = CourseSettings(course_id=course_id)
        db.session.add(settings)
    
    # Toggle the status
    settings.excluded = not settings.excluded
    db.session.commit()
    
    # Log action
    status = "excluded" if settings.excluded else "included"
    log = Log(action="TOGGLE_COURSE_EXCLUSION",
             description=f"Changed course '{course.code}' to {status}")
    db.session.add(log)
    db.session.commit()
    
    flash(f"Course '{course.code}' is now {status}.", 'success')
    return redirect(url_for('calculation.all_courses'))

@calculation_bp.route('/update_display_method', methods=['POST'])
def update_display_method():
    """Update display method preference in session"""
    if request.method == 'POST':
        display_method = request.form.get('display_method')
        if display_method in ['absolute', 'relative']:
            # Store the display method in session
            session['display_method'] = display_method
            return jsonify({'status': 'success', 'reload': True})
    return jsonify({'status': 'error'})

def calculate_student_exam_score_optimized(student_id, exam_id, scores_dict, questions, attendance_dict=None):
    """Calculate a student's total score for an exam using preloaded data.
    
    This improved version uses an explicit check for None so that a valid score of 0 is not skipped.
    It also checks if the student attended the exam and returns None if they didn't.
    If the student didn't attend a mandatory exam, it returns None to indicate they should be excluded.
    For non-mandatory exams, missing is treated as 0.
    """
    if not questions:
        return None
        
    # Get the exam to check if it's mandatory
    exam = Exam.query.get(exam_id)
    is_mandatory = exam and exam.is_mandatory
        
    # Check if the student attended the exam
    if attendance_dict is not None:
        # Use the attendance dict if provided
        attended = attendance_dict.get((student_id, exam_id), True)  # Default to True if no record
        if not attended:
            if is_mandatory:
                return None  # Return None for mandatory exams to signal absence (to be checked with makeup)
            else:
                return Decimal('0')  # Student didn't attend non-mandatory exam, treat as 0
    else:
        # Check the database directly if no dict provided
        attendance = StudentExamAttendance.query.filter_by(
            student_id=student_id, 
            exam_id=exam_id
        ).first()
        
        # If there's an attendance record and the student didn't attend
        if attendance and not attendance.attended:
            if is_mandatory:
                return None  # Return None for mandatory exams to signal absence
            else:
                return Decimal('0')  # Non-mandatory absences counted as 0

    total_score = Decimal('0')
    total_possible = Decimal('0')

    # Even if a student is marked as attended, we need to check if there are any scores
    # for this exam's questions. If there are no scores, and it's a mandatory exam,
    # we should return None.
    has_scores = False
    for question in questions:
        score_value = scores_dict.get((student_id, question.id, exam_id))
        # Use explicit check for None to include a valid score of 0
        if score_value is not None:
            has_scores = True
            total_score += Decimal(str(score_value))
        total_possible += question.max_score

    # If mandatory exam and no scores were found, exclude student
    if is_mandatory and not has_scores:
        return None

    if total_possible == Decimal('0'):
        return None

    return (total_score / total_possible) * Decimal('100')

def calculate_course_outcome_score_optimized(student_id, outcome_id, scores_dict, outcome_questions):
    """Calculate a student's score for a course outcome using preloaded data"""
    questions = outcome_questions.get(outcome_id, [])

    if not questions:
        return None # No questions linked to this outcome

    # 1. Calculate the total possible score from ALL relevant questions first.
    total_possible = Decimal('0')
    for q in questions:
        total_possible += q.max_score

    # Handle cases where an outcome might be linked only to 0-point questions
    if total_possible == Decimal('0'):
        # If total possible is 0, achievement is undefined or could be considered 100% if any score > 0 exists?
        # Returning None is safest, or 0 if preferred. Or check if any score > 0.
        # Let's check if any score exists and is positive for this outcome's questions.
        student_score_exists_positive = False
        for question in questions:
            exam_id = question.exam_id
            score_value = scores_dict.get((student_id, question.id, exam_id))
            if score_value is not None and score_value > 0:
                student_score_exists_positive = True
                break
        # If possible is 0, but student scored > 0 (extra credit?), return 100%. Otherwise 0 or None.
        # Returning 0 is a reasonable default if no points were possible and none were scored.
        return Decimal('100.0') if student_score_exists_positive else Decimal('0.0')

    # 2. Calculate the student's total score, treating missing scores as 0.
    total_score = Decimal('0')
    for question in questions:
        exam_id = question.exam_id
        # Use .get() which returns None if the key is not found
        score_value = scores_dict.get((student_id, question.id, exam_id))

        if score_value is not None: # Check if score exists - includes score of 0
            # Ensure score_value is Decimal for consistency
            total_score += Decimal(str(score_value))
        # Implicitly, if score_value is None, we add 0, which is correct.

    # 3. Calculate the percentage.
    # total_possible is guaranteed non-zero here due to the check above.
    return (total_score / total_possible) * Decimal('100')

# Optimized helper function to calculate a student's score for a program outcome
def calculate_program_outcome_score_optimized(student_id, outcome_id, course_id, scores_dict, 
                                             program_to_course_outcomes, outcome_questions):
    """Calculate a student's score for a program outcome using preloaded data"""
    # Get related course outcomes from the preloaded mapping
    related_course_outcomes = program_to_course_outcomes.get(outcome_id, [])
    
    if not related_course_outcomes:
        return None
    
    total_score = Decimal('0')
    count = 0
    
    for course_outcome in related_course_outcomes:
        score = calculate_course_outcome_score_optimized(
            student_id, course_outcome.id, scores_dict, outcome_questions
        )
        if score is not None:
            # Ensure score is a Decimal for consistent calculation
            if not isinstance(score, Decimal):
                score = Decimal(str(score))
            total_score += score
            count += 1
    
    if count == 0:
        return None
    
    # Return the average as a Decimal
    return total_score / Decimal(str(count))

@calculation_bp.route('/course/<int:course_id>/debug')
def debug_calculations(course_id):
    """Debug route to show calculation data"""
    course = Course.query.get_or_404(course_id)
    exams = Exam.query.filter_by(course_id=course_id, is_makeup=False).all()
    makeup_exams = Exam.query.filter_by(course_id=course_id, is_makeup=True).all()
    course_outcomes = CourseOutcome.query.filter_by(course_id=course_id).all()
    program_outcomes = ProgramOutcome.query.all()
    students = Student.query.filter_by(course_id=course_id).all()
    
    # Get exam weights
    weights = {}
    total_weight = Decimal('0')
    for exam in exams:
        weight = ExamWeight.query.filter_by(exam_id=exam.id).first()
        if weight:
            weights[exam.id] = weight.weight
            total_weight += weight.weight
        else:
            weights[exam.id] = Decimal('0')
    
    # Create a map of exams to their questions
    questions_by_exam = {}
    for exam in exams + makeup_exams:
        questions_by_exam[exam.id] = exam.questions
    
    # Create a map of course outcomes to their related questions
    outcome_questions = {}
    for co in course_outcomes:
        outcome_questions[co.id] = co.questions
    
    # Create a map of program outcomes to their related course outcomes
    program_to_course_outcomes = {}
    for po in program_outcomes:
        related_cos = [co for co in course_outcomes if po in co.program_outcomes]
        program_to_course_outcomes[po.id] = related_cos
    
    # Preload all scores for this course
    student_ids = [s.id for s in students]
    exam_ids = [e.id for e in exams + makeup_exams]
    scores_dict = {}
    
    if student_ids and exam_ids:
        scores = Score.query.filter(
            Score.student_id.in_(student_ids),
            Score.exam_id.in_(exam_ids)
        ).all()
        
        for score in scores:
            key = (score.student_id, score.question_id, score.exam_id)
            scores_dict[key] = score.score
    
    # Preload attendance information
    attendance_dict = {}
    if student_ids and exam_ids:
        attendances = StudentExamAttendance.query.filter(
            StudentExamAttendance.student_id.in_(student_ids),
            StudentExamAttendance.exam_id.in_(exam_ids)
        ).all()
        
        for attendance in attendances:
            attendance_dict[(attendance.student_id, attendance.exam_id)] = attendance.attended
    
    # Calculate student results
    student_results = {}
    for student in students:
        # Initialize results for this student
        student_results[student.id] = {
            'student': student,
            'exam_scores': {},  # Raw scores per exam
            'weighted_score': Decimal('0'),  # Final weighted score
            'course_outcome_scores': {},  # Scores per course outcome
            'program_outcome_scores': {},  # Scores per program outcome
            'skip': False  # Track if student should be excluded from calculations
        }
        
        # Check if student should be excluded due to mandatory exam policy
        if mandatory_exams:
            skip_student = False
            for exam in mandatory_exams:
                # Check if student attended the regular exam
                regular_attended = attendance_dict.get((student.id, exam.id), True)
                regular_score = None
                if regular_attended:
                    regular_score = calculate_student_exam_score_optimized(
                        student.id, exam.id, scores_dict, questions_by_exam[exam.id], attendance_dict
                    )
                
                # Check if there's a makeup exam for this mandatory exam
                makeup_exam = next((m for m in makeup_exams if m.makeup_for == exam.id), None)
                makeup_attended = False
                makeup_score = None
                
                if makeup_exam:
                    makeup_attended = attendance_dict.get((student.id, makeup_exam.id), False)
                    if makeup_attended:
                        makeup_score = calculate_student_exam_score_optimized(
                            student.id, makeup_exam.id, scores_dict, questions_by_exam[makeup_exam.id], attendance_dict
                        )
                
                # Skip student if they missed both the mandatory exam and its makeup (if exists)
                if (not regular_attended or regular_score is None) and (not makeup_exam or not makeup_attended or makeup_score is None):
                    skip_student = True
                    break
            
            if skip_student:
                student_results[student.id]['skip'] = True
                student_results[student.id]['missing_mandatory'] = True
                student_results[student.id]['overall_percentage'] = 0
                student_results[student.id] = student_results[student.id]
                continue
        
        # Calculate exam scores for student
        for exam in exams:
            # Check if there's a makeup for this exam
            makeup_exam = next((m for m in makeup_exams if m.makeup_for == exam.id), None)
            
            # If there's a makeup exam, check if student attended it
            if makeup_exam:
                makeup_attended = attendance_dict.get((student.id, makeup_exam.id), False)
                
                # If student attended the makeup, use that score exclusively
                if makeup_attended:
                    makeup_score = calculate_student_exam_score_optimized(
                        student.id, makeup_exam.id, scores_dict, 
                        questions_by_exam[makeup_exam.id], attendance_dict
                    )
                    # Use makeup score even if it's 0 (as long as it's not None)
                    if makeup_score is not None:
                        student_results[student.id]['exam_scores'][exam.id] = makeup_score
                        continue
            
            # If no makeup was attended or makeup score is None, use regular exam score
            exam_score = calculate_student_exam_score_optimized(
                student.id, exam.id, scores_dict,
                questions_by_exam[exam.id], attendance_dict
            )
            if exam_score is not None:
                student_results[student.id]['exam_scores'][exam.id] = exam_score
        
        # Calculate weighted score using normalized weights
        weighted_score = Decimal('0')
        total_used_weight = Decimal('0')
        
        for exam_id, score in student_results[student.id]['exam_scores'].items():
            # Ensure weight exists before calculation
            if exam_id in weights:
                weighted_score += score * weights[exam_id]
                total_used_weight += weights[exam_id]
            else:
                print(f"Debug Warning: Missing weight for exam_id {exam_id} for student {student.id}")

        # Normalize the weighted score if total_used_weight doesn't equal total_weight
        if total_used_weight > Decimal('0') and total_used_weight != total_weight:
            weighted_score = weighted_score / total_used_weight * total_weight

        student_results[student.id]['weighted_score'] = weighted_score
        
        # Calculate course outcome scores
        for outcome in course_outcomes:
            score = calculate_course_outcome_score_optimized(student.id, outcome.id, scores_dict, outcome_questions)
            student_results[student.id]['course_outcome_scores'][outcome.id] = score
        
        # Calculate program outcome scores
        for outcome in program_outcomes:
            score = calculate_program_outcome_score_optimized(student.id, outcome.id, course_id, scores_dict, program_to_course_outcomes, outcome_questions)
            student_results[student.id]['program_outcome_scores'][outcome.id] = score
    
    # Calculate average scores for the whole class
    class_results = {
        'course_outcome_scores': {},
        'program_outcome_scores': {}
    }
    
    # Calculate average course outcome scores
    for outcome in course_outcomes:
        scores = [r['course_outcome_scores'][outcome.id] for r in student_results.values() 
                 if r['course_outcome_scores'][outcome.id] is not None]
        if scores:
            class_results['course_outcome_scores'][outcome.id] = sum(scores) / len(scores)
        else:
            class_results['course_outcome_scores'][outcome.id] = None
    
    # Calculate average program outcome scores
    for outcome in program_outcomes:
        scores = [r['program_outcome_scores'][outcome.id] for r in student_results.values() 
                 if r['program_outcome_scores'][outcome.id] is not None]
        if scores:
            class_results['program_outcome_scores'][outcome.id] = sum(scores) / len(scores)
        else:
            class_results['program_outcome_scores'][outcome.id] = None
    
    # Create course_outcome_results and program_outcome_results for template
    course_outcome_results = {}
    program_outcome_results = {}
    
    # Format course outcome results
    for outcome in course_outcomes:
        avg_score = class_results['course_outcome_scores'].get(outcome.id)
        course_outcome_results[outcome.code] = {
            'description': outcome.description,
            'percentage': avg_score if avg_score is not None else 0,
            'program_outcomes': [po.code for po in outcome.program_outcomes],
            'achievement_level': get_achievement_level(float(avg_score) if avg_score is not None else 0, achievement_levels)
        }
    
    # Format program outcome results
    for outcome in program_outcomes:
        avg_score = class_results['program_outcome_scores'].get(outcome.id)
        
        # Check if this program outcome is linked to any course outcomes for this course
        contributes = False
        for co in course.course_outcomes:
            if outcome in co.program_outcomes:
                contributes = True
                break
                
        program_outcome_results[outcome.code] = {
            'description': outcome.description,
            'percentage': avg_score if avg_score is not None else 0,
            'contributes': contributes,
            'achievement_level': get_achievement_level(float(avg_score) if avg_score is not None else 0, achievement_levels)
        }
    
    # Format student results for template
    formatted_student_results = {}
    for student_id, data in student_results.items():
        student = data['student']
        
        # Format course outcome scores - ensure percentages are numbers not dictionaries
        course_outcome_scores = {}
        for outcome in course_outcomes:
            percentage = data['course_outcome_scores'].get(outcome.id)
            course_outcome_scores[outcome.code] = percentage if percentage is not None else 0
        
        # Format exam scores to include names
        exam_scores = {}
        for exam_id, score in data['exam_scores'].items():
            exam = next((e for e in exams if e.id == exam_id), None)
            if exam:
                exam_scores[exam.name] = score
        
        formatted_student_results[student_id] = {
            'student_id': student.student_id,
            'name': f"{student.first_name} {student.last_name}".strip(),
            'overall_percentage': data['weighted_score'],
            'course_outcomes': course_outcome_scores,
            'exam_scores': exam_scores
        }
    
    # Check if questions are linked to course outcomes
    has_exam_questions = False
    for exam in exams:
        questions = Question.query.filter_by(exam_id=exam.id).all()
        for question in questions:
            if question.course_outcomes:
                has_exam_questions = True
                break
        if has_exam_questions:
            break
    
    # Check if we have student scores
    has_student_scores = Score.query.join(Question).join(Exam).filter(Exam.course_id == course_id).first() is not None
    
    # Check if weights are valid
    has_valid_weights = abs(total_weight - Decimal('1.0')) <= Decimal('0.01')
   
    # Create a debug response
    debug_info = {
        'course': {
            'id': course.id,
            'code': course.code,
            'name': course.name
        },
        'exams': [{'id': exam.id, 'name': exam.name} for exam in exams],
        'makeup_exams': [{'id': exam.id, 'name': exam.name} for exam in makeup_exams],
        'course_outcomes': [{'id': co.id, 'code': co.code} for co in course_outcomes],
        'program_outcomes': [{'id': po.id, 'code': po.code} for po in program_outcomes],
        'students': [{'id': s.id, 'student_id': s.student_id, 'name': f"{s.first_name} {s.last_name}"} for s in students],
        'weights': {str(k): float(v) for k, v in weights.items()},
        'total_weight': float(total_weight),
        'has_exam_questions': has_exam_questions,
        'has_student_scores': has_student_scores,
        'has_valid_weights': has_valid_weights,
        'scores_count': len(scores_dict),
        'student_results_count': len(student_results),
        'formatted_student_results_count': len(formatted_student_results),
        'course_outcome_results': {k: {
            'description': v['description'][:30] + '...' if len(v['description']) > 30 else v['description'],
            'percentage': float(v['percentage']),
            'program_outcomes_count': len(v['program_outcomes'])
        } for k, v in course_outcome_results.items()},
        'program_outcome_results': {k: {
            'description': v['description'][:30] + '...' if len(v['description']) > 30 else v['description'],
            'percentage': float(v['percentage']),
            'course_outcomes_count': len(v['course_outcomes'])
        } for k, v in program_outcome_results.items()}
    }
    
    # Format as pre-formatted text
    import json
    debug_html = f"<pre>{json.dumps(debug_info, indent=4)}</pre>"
    
    # Generate chart data to check if it's valid
    chart_data = {
        'po_labels': [po_code for po_code in program_outcome_results],
        'po_data': [float(po_data['percentage']) for _, po_data in program_outcome_results.items()],
        'co_labels': [co_code for co_code in course_outcome_results],
        'co_data': [float(co_data['percentage']) for _, co_data in course_outcome_results.items()],
    }
    
    chart_html = f"<h2>Chart Data</h2><pre>{json.dumps(chart_data, indent=4)}</pre>"
    
    # Check the JS script that would be generated for charts
    po_chart_js = """
    <script>
        // Program Outcomes Chart
        const poCtx = document.getElementById('programOutcomesChart').getContext('2d');
        const poChart = new Chart(poCtx, {
            type: 'bar',
            data: {
                labels: [
                    """ + ", ".join([f"'{po_code}'" for po_code in program_outcome_results]) + """
                ],
                datasets: [{
                    label: 'Achievement Level (%)',
                    data: [
                        """ + ", ".join([f"{po_data['percentage']:.1f}" for _, po_data in program_outcome_results.items()]) + """
                    ],
                    backgroundColor: [
                        """ + ", ".join([f"'rgba(40, 167, 69, 0.7)'" if po_data['percentage'] >= 70 else 
                                       f"'rgba(255, 193, 7, 0.7)'" if po_data['percentage'] >= 50 else
                                       f"'rgba(220, 53, 69, 0.7)'" for _, po_data in program_outcome_results.items()]) + """
                    ],
                    borderColor: [
                        """ + ", ".join([f"'rgba(40, 167, 69, 1)'" if po_data['percentage'] >= 70 else 
                                       f"'rgba(255, 193, 7, 1)'" if po_data['percentage'] >= 50 else
                                       f"'rgba(220, 53, 69, 1)'" for _, po_data in program_outcome_results.items()]) + """
                    ],
                    borderWidth: 1
                }]
            },
            options: {
                scales: {
                    y: {
                        beginAtZero: true,
                        max: 100,
                        title: {
                            display: true,
                            text: 'Achievement Level (%)'
                        }
                    },
                    x: {
                        title: {
                            display: true,
                            text: 'Program Outcomes'
                        }
                    }
                },
                plugins: {
                    title: {
                        display: true,
                        text: 'Program Outcome Achievement Levels',
                        font: {
                            size: 16
                        }
                    }
                }
            }
        });
    </script>
    """
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Debug Calculation Data</title>
        <style>
            pre {{ max-height: 500px; overflow: auto; }}
        </style>
    </head>
    <body>
        <h1>Debug Calculation Data for {course.code}</h1>
        {debug_html}
        
        {chart_html}
        
        <h2>Generated Chart.js code (for Program Outcomes)</h2>
        <pre>{po_chart_js}</pre>
    </body>
    </html>
    """ 

@calculation_bp.route('/course/<int:course_id>/export_exams')
def export_course_exams(course_id):
    """Export exam details for a course to CSV, including proper makeup exam relationships"""
    course = Course.query.get_or_404(course_id)
    
    # Get all exams for this course
    all_exams = Exam.query.filter_by(course_id=course_id).all()
    
    # Fix any makeup exams with missing relationships
    makeup_exams = [exam for exam in all_exams if exam.is_makeup and exam.makeup_for is None]
    for makeup_exam in makeup_exams:
        name = makeup_exam.name.lower()
        if name.startswith("makeup"):
            base_name = name[6:].strip()  # Remove "Makeup" and any spaces
            
            # Find a matching exam
            for exam in all_exams:
                if not exam.is_makeup and exam.name.lower() == base_name:
                    makeup_exam.makeup_for = exam.id
                    db.session.add(makeup_exam)
                    break
            
            # If exact match not found, try fuzzy matching
            if makeup_exam.makeup_for is None:
                for exam in all_exams:
                    if not exam.is_makeup and base_name in exam.name.lower():
                        makeup_exam.makeup_for = exam.id
                        db.session.add(makeup_exam)
                        break
    
    # Commit any fixes
    if makeup_exams:
        try:
            db.session.commit()
            print(f"Fixed {len(makeup_exams)} makeup exam relationships")
        except Exception as e:
            db.session.rollback()
            print(f"Error fixing makeup exams: {str(e)}")
    
    # Prepare data for export
    headers = ['Exam Name', 'Max Score', 'Date', 'Question Count', 'Is Makeup', 'Makeup For']
    
    # Create a mapping of exam IDs to exam names for lookup
    exam_name_map = {exam.id: exam.name for exam in all_exams}
    
    # Add exam data as a list of dictionaries
    data = []
    
    for exam in all_exams:
        question_count = Question.query.filter_by(exam_id=exam.id).count()
        
        # Get makeup information
        is_makeup = "Yes" if exam.is_makeup else "No"
        makeup_for = "N/A"
        
        # If this is a makeup exam, get the original exam name
        if exam.is_makeup and exam.makeup_for:
            makeup_for = exam_name_map.get(exam.makeup_for, "N/A")
        
        # Create a dictionary for this exam
        exam_data = {
            'Exam Name': exam.name,
            'Max Score': float(exam.max_score) if exam.max_score else 0,
            'Date': exam.exam_date.strftime('%m/%d/%Y') if exam.exam_date else 'N/A',
            'Question Count': question_count,
            'Is Makeup': is_makeup,
            'Makeup For': makeup_for
        }
        
        data.append(exam_data)
    
    # Log export action
    log = Log(action="EXPORT_EXAMS", 
             description=f"Exported exam details for course: {course.code}")
    db.session.add(log)
    db.session.commit()
    
    # Export data as a list of dictionaries
    return export_to_excel_csv(data, f"exams_{course.code}", headers)

@calculation_bp.route('/all_utilities')
def all_utilities():
    """Redirect to the utilities page"""
    return redirect(url_for('utility.index'))

@calculation_bp.route('/course/<int:course_id>/achievement-levels', methods=['GET', 'POST'])
def manage_achievement_levels(course_id):
    """Manage achievement levels for a course"""
    course = Course.query.get_or_404(course_id)
    achievement_levels = AchievementLevel.query.filter_by(course_id=course_id).order_by(AchievementLevel.min_score.desc()).all()
    
    if request.method == 'POST':
        # Process form submission
        action = request.form.get('action')
        level_id = request.form.get('level_id')
        
        if action == 'save':
            # Validate form input
            name = request.form.get('name', '').strip()
            min_score = request.form.get('min_score', '')
            max_score = request.form.get('max_score', '')
            color = request.form.get('color', '')
            
            # Validate inputs
            if not name:
                flash("Level name is required", "error")
                return redirect(url_for('calculation.manage_achievement_levels', course_id=course_id))
            
            try:
                min_score = float(min_score)
                max_score = float(max_score)
                
                if min_score < 0 or min_score > 100:
                    flash("Minimum score must be between 0 and 100", "error")
                    return redirect(url_for('calculation.manage_achievement_levels', course_id=course_id))
                
                if max_score < 0 or max_score > 100:
                    flash("Maximum score must be between 0 and 100", "error")
                    return redirect(url_for('calculation.manage_achievement_levels', course_id=course_id))
                
                if min_score >= max_score:
                    flash("Minimum score must be less than maximum score", "error")
                    return redirect(url_for('calculation.manage_achievement_levels', course_id=course_id))
                
            except ValueError:
                flash("Score values must be valid numbers", "error")
                return redirect(url_for('calculation.manage_achievement_levels', course_id=course_id))
            
            # Validate color
            valid_colors = ['primary', 'secondary', 'success', 'danger', 'warning', 'info', 'dark']
            if color not in valid_colors:
                flash("Invalid color selected", "error")
                return redirect(url_for('calculation.manage_achievement_levels', course_id=course_id))
            
            # Check for overlapping ranges with other levels
            query = AchievementLevel.query.filter_by(course_id=course_id)
            if level_id:
                query = query.filter(AchievementLevel.id != level_id)
            
            for existing_level in query.all():
                # Check if ranges overlap
                if (min_score <= existing_level.max_score and max_score >= existing_level.min_score):
                    flash(f"Score range overlaps with existing level '{existing_level.name}' ({existing_level.min_score}% - {existing_level.max_score}%)", "error")
                    return redirect(url_for('calculation.manage_achievement_levels', course_id=course_id))
            
            # Save new or update existing
            try:
                if level_id:
                    level = AchievementLevel.query.get_or_404(level_id)
                    level.name = name
                    level.min_score = min_score
                    level.max_score = max_score
                    level.color = color
                    level.updated_at = datetime.now()
                    
                    # Log action
                    log_action = "EDIT_ACHIEVEMENT_LEVEL"
                    log_description = f"Edited achievement level '{name}' for course: {course.code}"
                else:
                    level = AchievementLevel(
                        course_id=course_id,
                        name=name,
                        min_score=min_score,
                        max_score=max_score,
                        color=color
                    )
                    db.session.add(level)
                    
                    # Log action
                    log_action = "ADD_ACHIEVEMENT_LEVEL"
                    log_description = f"Added achievement level '{name}' to course: {course.code}"
                
                # Log action
                log = Log(action=log_action, description=log_description)
                db.session.add(log)
                
                db.session.commit()
                flash(f"Achievement level '{name}' saved successfully", 'success')
            except Exception as e:
                db.session.rollback()
                logging.error(f"Error saving achievement level: {str(e)}")
                flash(f"Error saving achievement level: {str(e)}", 'error')
            
            return redirect(url_for('calculation.manage_achievement_levels', course_id=course_id))
            
        elif action == 'delete':
            try:
                level = AchievementLevel.query.get_or_404(level_id)
                level_name = level.name
                
                # Log action before deletion
                log = Log(action="DELETE_ACHIEVEMENT_LEVEL", 
                         description=f"Deleted achievement level '{level_name}' from course: {course.code}")
                db.session.add(log)
                
                db.session.delete(level)
                db.session.commit()
                flash(f"Achievement level '{level_name}' deleted successfully", 'success')
            except Exception as e:
                db.session.rollback()
                logging.error(f"Error deleting achievement level: {str(e)}")
                flash(f"Error deleting achievement level: {str(e)}", 'error')
            
            return redirect(url_for('calculation.manage_achievement_levels', course_id=course_id))
    
    # For GET request, check if we need to set up default levels
    if not achievement_levels:
        # Add default achievement levels
        default_levels = [
            {"name": "Excellent", "min_score": 90.00, "max_score": 100.00, "color": "success"},
            {"name": "Better", "min_score": 70.00, "max_score": 89.99, "color": "info"},
            {"name": "Good", "min_score": 60.00, "max_score": 69.99, "color": "primary"},
            {"name": "Need Improvements", "min_score": 50.00, "max_score": 59.99, "color": "warning"},
            {"name": "Failure", "min_score": 0.01, "max_score": 49.99, "color": "danger"}
        ]
        
        for level_data in default_levels:
            level = AchievementLevel(
                course_id=course_id,
                name=level_data["name"],
                min_score=level_data["min_score"],
                max_score=level_data["max_score"],
                color=level_data["color"]
            )
            db.session.add(level)
        
        # Log action
        log = Log(action="ADD_DEFAULT_ACHIEVEMENT_LEVELS", 
                 description=f"Added default achievement levels to course: {course.code}")
        db.session.add(log)
        db.session.commit()
        
        # Refresh achievement levels
        achievement_levels = AchievementLevel.query.filter_by(course_id=course_id).order_by(AchievementLevel.min_score.desc()).all()
    
    return render_template('calculation/achievement_levels.html', 
                         course=course, 
                         achievement_levels=achievement_levels, 
                         active_page='courses') 