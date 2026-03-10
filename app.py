from flask import Flask, render_template, redirect, request, url_for, flash, abort, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime, timedelta
from functools import wraps
import os
import uuid

app = Flask(__name__)
app.secret_key = "supersecretkey"

# ---------------- MongoDB Setup ----------------
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI )
db = client["smart_notice_board"]
users_collection = db["users"]
notices_collection = db["notices"]
analytics_collection = db["analytics"]

# ---------------- Upload Configuration ----------------
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB max file size

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ---------------- Flask Login ----------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# ---------------- Role Decorator ----------------
def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return wrapper
    return decorator

# ---------------- User Class ----------------
class User(UserMixin):
    def __init__(self, user_data):
        self.id = str(user_data["_id"])
        self.username = user_data["username"]
        self.email = user_data.get("email", "")
        self.role = user_data["role"]

@login_manager.user_loader
def load_user(user_id):
    try:
        user = users_collection.find_one({"_id": ObjectId(user_id)})
        if user:
            return User(user)
    except:
        return None

# ---------------- Helper Functions ----------------
def log_activity(action, user, notice_id=None, details=None):
    """Log user activities for analytics"""
    analytics_collection.insert_one({
        "action": action,
        "user": user,
        "notice_id": notice_id,
        "details": details,
        "timestamp": datetime.utcnow()
    })

def get_notice_stats():
    """Get statistics about notices"""
    total = notices_collection.count_documents({"is_active": True})
    urgent = notices_collection.count_documents({"category": "Urgent", "is_active": True})
    
    # Get notices from this week
    week_ago = datetime.utcnow() - timedelta(days=7)
    this_week = notices_collection.count_documents({
        "date_posted": {"$gte": week_ago},
        "is_active": True
    })
    
    return {
        "total": total,
        "urgent": urgent,
        "this_week": this_week,
        "categories": 4
    }

# ---------------- Dashboard ----------------
@app.route('/')
@login_required
def dashboard():
    notices = list(notices_collection.find({"is_active": True}).sort("date_posted", -1))
    stats = get_notice_stats()
    log_activity("view_dashboard", current_user.username)
    return render_template("dashboard.html", notices=notices, stats=stats)

# ---------------- Signup ----------------
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = generate_password_hash(request.form['password'])
        role = request.form['role']

        if users_collection.find_one({"email": email}):
            flash("Email already registered!")
            return redirect(url_for('signup'))

        users_collection.insert_one({
            "username": username,
            "email": email,
            "password": password,
            "role": role,
            "created_at": datetime.utcnow(),
            "is_active": True
        })

        log_activity("user_signup", username, details={"role": role})
        flash("Account created! Please login.")
        return redirect(url_for('login'))

    return render_template("sign.html")

# ---------------- Login ----------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        user = users_collection.find_one({"email": email})

        if user and check_password_hash(user["password"], password):
            if user.get("is_active", True):
                login_user(User(user))
                log_activity("user_login", user["username"])
                return redirect(url_for('dashboard'))
            else:
                flash("Your account has been deactivated. Contact admin.")
        else:
            flash("Invalid credentials")

    return render_template("login.html")

# ---------------- Post Notice ----------------
@app.route('/post', methods=['GET', 'POST'])
@login_required
@role_required("admin", "teacher")
def post_notice():
    if request.method == 'POST':
        title = request.form['title']
        message = request.form['message']
        category = request.form.get('category', 'General')
        schedule_date = request.form.get('schedule_date')
        priority = request.form.get('priority', 'low')
        
        # Handle file uploads
        image_filename = None
        pdf_filename = None
        
        # Image upload
        if 'image' in request.files:
            image_file = request.files['image']
            if image_file and image_file.filename != '' and allowed_file(image_file.filename):
                unique_name = str(uuid.uuid4()) + "_" + secure_filename(image_file.filename)
                image_file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_name))
                image_filename = unique_name
        
        # PDF upload
        if 'pdf' in request.files:
            pdf_file = request.files['pdf']
            if pdf_file and pdf_file.filename != '' and allowed_file(pdf_file.filename):
                unique_name = str(uuid.uuid4()) + "_" + secure_filename(pdf_file.filename)
                pdf_file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_name))
                pdf_filename = unique_name

        notice_data = {
            "title": title,
            "message": message,
            "category": category,
            "priority": priority,
            "posted_by": current_user.username,
            "role": current_user.role,
            "image": image_filename,
            "pdf": pdf_filename,
            "date_posted": datetime.utcnow(),
            "is_active": True,
            "views": 0,
            "likes": 0
        }
        
        if schedule_date:
            notice_data["schedule_date"] = datetime.strptime(schedule_date, '%Y-%m-%dT%H:%M')
            notice_data["is_scheduled"] = True
        
        result = notices_collection.insert_one(notice_data)
        
        log_activity("post_notice", current_user.username, 
                    notice_id=str(result.inserted_id),
                    details={"title": title, "category": category})

        flash("Notice Posted Successfully!")
        return redirect(url_for('dashboard'))

    return render_template("post_notice.html")

# ---------------- My Notices ----------------
@app.route('/my-notices')
@login_required
def my_notices():
    notices = list(notices_collection.find({
        "posted_by": current_user.username,
        "is_active": True
    }).sort("date_posted", -1))
    
    return render_template("my_notices.html", notices=notices)

# ---------------- Archive ----------------
@app.route('/archive')
@login_required
def archive():
    # Get notices older than 30 days or marked as archived
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    archived_notices = list(notices_collection.find({
        "$or": [
            {"is_active": False},
            {"date_posted": {"$lt": thirty_days_ago}}
        ]
    }).sort("date_posted", -1))
    
    return render_template("archive.html", notices=archived_notices)

# ---------------- Calendar View ----------------
@app.route('/calendar')
@login_required
def calendar():
    # Get all active notices with their dates
    notices = list(notices_collection.find({"is_active": True}).sort("date_posted", -1))
    
    # Convert notices to calendar events format
    events = []
    for notice in notices:
        events.append({
            "id": str(notice["_id"]),
            "title": notice["title"],
            "start": notice["date_posted"].strftime("%Y-%m-%d"),
            "category": notice.get("category", "General"),
            "priority": notice.get("priority", "low")
        })
    
    return render_template("calendar.html", events=events)

# ---------------- Analytics (Admin Only) ----------------
@app.route('/analytics')
@login_required
@role_required("admin")
def analytics():
    # Get various analytics data
    
    # Total users by role
    users_by_role = {}
    for role in ["admin", "teacher", "student"]:
        count = users_collection.count_documents({"role": role, "is_active": True})
        users_by_role[role] = count
    
    # Notices by category
    notices_by_category = {}
    for category in ["Academic", "Events", "Urgent", "General"]:
        count = notices_collection.count_documents({"category": category, "is_active": True})
        notices_by_category[category] = count
    
    # Recent activities
    recent_activities = list(analytics_collection.find().sort("timestamp", -1).limit(20))
    
    # Notices posted per day (last 7 days)
    daily_posts = []
    for i in range(7):
        day = datetime.utcnow() - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        count = notices_collection.count_documents({
            "date_posted": {"$gte": day_start, "$lte": day_end}
        })
        
        daily_posts.append({
            "date": day.strftime("%Y-%m-%d"),
            "count": count
        })
    
    stats = {
        "users_by_role": users_by_role,
        "notices_by_category": notices_by_category,
        "recent_activities": recent_activities,
        "daily_posts": daily_posts,
        "total_users": sum(users_by_role.values()),
        "total_notices": notices_collection.count_documents({"is_active": True}),
        "total_activities": analytics_collection.count_documents({})
    }
    
    return render_template("analytics.html", stats=stats)

# ---------------- User Management (Admin Only) ----------------
@app.route('/users')
@login_required
@role_required("admin")
def manage_users():
    users = list(users_collection.find({"is_active": True}).sort("created_at", -1))
    return render_template("manage_users.html", users=users)

@app.route('/users/deactivate/<user_id>')
@login_required
@role_required("admin")
def deactivate_user(user_id):
    user = users_collection.find_one({"_id": ObjectId(user_id)})
    if user:
        users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"is_active": False}}
        )
        log_activity("deactivate_user", current_user.username, 
                    details={"target_user": user["username"]})
        flash(f"User {user['username']} deactivated successfully!")
    return redirect(url_for('manage_users'))

@app.route('/users/activate/<user_id>')
@login_required
@role_required("admin")
def activate_user(user_id):
    user = users_collection.find_one({"_id": ObjectId(user_id)})
    if user:
        users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"is_active": True}}
        )
        log_activity("activate_user", current_user.username,
                    details={"target_user": user["username"]})
        flash(f"User {user['username']} activated successfully!")
    return redirect(url_for('manage_users'))

# ---------------- Settings ----------------
@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        # Update user settings
        new_username = request.form.get('username')
        new_email = request.form.get('email')
        
        update_data = {}
        if new_username:
            update_data["username"] = new_username
        if new_email:
            # Check if email is already taken
            existing = users_collection.find_one({
                "email": new_email,
                "_id": {"$ne": ObjectId(current_user.id)}
            })
            if not existing:
                update_data["email"] = new_email
            else:
                flash("Email already taken!")
                return redirect(url_for('settings'))
        
        if update_data:
            users_collection.update_one(
                {"_id": ObjectId(current_user.id)},
                {"$set": update_data}
            )
            log_activity("update_settings", current_user.username)
            flash("Settings updated successfully!")
        
        # Handle password change
        if request.form.get('current_password') and request.form.get('new_password'):
            user = users_collection.find_one({"_id": ObjectId(current_user.id)})
            if check_password_hash(user["password"], request.form['current_password']):
                new_password = generate_password_hash(request.form['new_password'])
                users_collection.update_one(
                    {"_id": ObjectId(current_user.id)},
                    {"$set": {"password": new_password}}
                )
                flash("Password changed successfully!")
            else:
                flash("Current password is incorrect!")
        
        return redirect(url_for('settings'))
    
    user = users_collection.find_one({"_id": ObjectId(current_user.id)})
    return render_template("settings.html", user=user)

# ---------------- Edit Notice ----------------
@app.route('/edit/<notice_id>', methods=['GET', 'POST'])
@login_required
@role_required("admin", "teacher")
def edit_notice(notice_id):
    notice = notices_collection.find_one({"_id": ObjectId(notice_id)})

    if not notice:
        flash("Notice not found")
        return redirect(url_for('dashboard'))

    # Teacher can edit only their own notice
    if current_user.role == "teacher" and notice["posted_by"] != current_user.username:
        flash("You can edit only your own notices!")
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        updated_title = request.form['title']
        updated_message = request.form['message']
        updated_category = request.form.get('category', notice.get('category', 'General'))

        update_data = {
            "title": updated_title,
            "message": updated_message,
            "category": updated_category,
            "updated_at": datetime.utcnow()
        }

        # Handle new file uploads
        if 'image' in request.files:
            image_file = request.files['image']
            if image_file and image_file.filename != '' and allowed_file(image_file.filename):
                unique_name = str(uuid.uuid4()) + "_" + secure_filename(image_file.filename)
                image_file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_name))
                update_data["image"] = unique_name

        if 'pdf' in request.files:
            pdf_file = request.files['pdf']
            if pdf_file and pdf_file.filename != '' and allowed_file(pdf_file.filename):
                unique_name = str(uuid.uuid4()) + "_" + secure_filename(pdf_file.filename)
                pdf_file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_name))
                update_data["pdf"] = unique_name

        notices_collection.update_one(
            {"_id": ObjectId(notice_id)},
            {"$set": update_data}
        )

        log_activity("edit_notice", current_user.username,
                    notice_id=notice_id,
                    details={"title": updated_title})

        flash("Notice Updated Successfully!")
        return redirect(url_for('dashboard'))

    return render_template("edit_notice.html", notice=notice)

# ---------------- Delete Notice ----------------
@app.route('/delete/<notice_id>')
@login_required
@role_required("admin")
def delete_notice(notice_id):
    notice = notices_collection.find_one({"_id": ObjectId(notice_id)})

    if notice:
        # Delete associated files
        if notice.get("image"):
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], notice["image"])
            if os.path.exists(file_path):
                os.remove(file_path)
        
        if notice.get("pdf"):
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], notice["pdf"])
            if os.path.exists(file_path):
                os.remove(file_path)

        notices_collection.delete_one({"_id": ObjectId(notice_id)})
        
        log_activity("delete_notice", current_user.username,
                    notice_id=notice_id,
                    details={"title": notice["title"]})

    flash("Notice Deleted Successfully!")
    return redirect(url_for('dashboard'))

# ---------------- Archive/Unarchive Notice ----------------
@app.route('/toggle-archive/<notice_id>')
@login_required
@role_required("admin", "teacher")
def toggle_archive(notice_id):
    notice = notices_collection.find_one({"_id": ObjectId(notice_id)})
    
    if notice:
        if current_user.role == "teacher" and notice["posted_by"] != current_user.username:
            flash("You can only archive your own notices!")
            return redirect(url_for('dashboard'))
        
        new_status = not notice.get("is_active", True)
        notices_collection.update_one(
            {"_id": ObjectId(notice_id)},
            {"$set": {"is_active": new_status}}
        )
        
        action = "unarchive" if new_status else "archive"
        log_activity(f"{action}_notice", current_user.username, notice_id=notice_id)
        
        flash(f"Notice {'unarchived' if new_status else 'archived'} successfully!")
    
    return redirect(request.referrer or url_for('dashboard'))

# ---------------- Like Notice ----------------
@app.route('/like/<notice_id>')
@login_required
def like_notice(notice_id):
    notices_collection.update_one(
        {"_id": ObjectId(notice_id)},
        {"$inc": {"likes": 1}}
    )
    log_activity("like_notice", current_user.username, notice_id=notice_id)
    return jsonify({"success": True})

# ---------------- Search API ----------------
@app.route('/api/search')
@login_required
def search_notices():
    query = request.args.get('q', '')
    category = request.args.get('category', '')
    
    search_filter = {"is_active": True}
    
    if query:
        search_filter["$or"] = [
            {"title": {"$regex": query, "$options": "i"}},
            {"message": {"$regex": query, "$options": "i"}},
            {"posted_by": {"$regex": query, "$options": "i"}}
        ]
    
    if category and category != "all":
        search_filter["category"] = category
    
    notices = list(notices_collection.find(search_filter).sort("date_posted", -1))
    
    # Convert ObjectId to string for JSON serialization
    for notice in notices:
        notice["_id"] = str(notice["_id"])
        notice["date_posted"] = notice["date_posted"].isoformat()
    
    return jsonify(notices)

# ---------------- Logout ----------------
@app.route('/logout')
@login_required
def logout():
    log_activity("user_logout", current_user.username)
    logout_user()
    return redirect(url_for('login'))

# ---------------- Error Handlers ----------------
@app.errorhandler(403)
def forbidden(e):
    return render_template("403.html"), 403

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

@app.errorhandler(500)
def internal_error(e):
    return render_template("500.html"), 500

# ---------------- Context Processor ----------------
@app.context_processor
def utility_processor():
    def get_stats():
        return get_notice_stats()
    return dict(get_stats=get_stats)

# ---------------- Run App ----------------
if __name__ == "__main__":
    app.run(debug=True)