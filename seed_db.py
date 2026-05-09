import random
from backend.database.db import SessionLocal, engine, Base
from backend.database.models import Employee

def seed_db():
    print("Creating tables...")
    Base.metadata.create_all(bind=engine)
    
    db = SessionLocal()
    count = db.query(Employee).count()
    if count > 0:
        print(f"Database already seeded with {count} employees. Skipping.")
        db.close()
        return

    print("Seeding database with employee data...")
    departments = ["Sales", "Engineering", "Marketing", "HR", "Finance", "Product"]
    
    # Generate 50 realistic employee records
    for i in range(1, 51):
        dep = random.choice(departments)
        if dep in ["Engineering", "Product"]:
            salary = random.randint(80000, 160000)
            attrition = random.uniform(0.1, 0.4)
        elif dep in ["Sales", "Marketing"]:
            salary = random.randint(60000, 120000)
            attrition = random.uniform(0.3, 0.7)
        else:
            salary = random.randint(50000, 100000)
            attrition = random.uniform(0.1, 0.5)

        emp = Employee(
            name=f"Emp_{i}",
            department=dep,
            performance_score=random.randint(1, 10),
            attendance_pct=round(random.uniform(80.0, 100.0), 1),
            attrition_score=round(attrition, 2),
            salary=salary
        )
        db.add(emp)
    
    db.commit()
    db.close()
    print("Database seeding completed.")

if __name__ == "__main__":
    seed_db()
