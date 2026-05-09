# from sqlalchemy import Column, Integer, String, Float
# from backend.database.db import Base

# class User(Base):
#     __tablename__ = "users"

#     id = Column(Integer, primary_key=True, index=True)
#     username = Column(String, unique=True, index=True, nullable=False)
#     hashed_password = Column(String, nullable=False)


# class Employee(Base):
#     __tablename__ = "employees"

#     id = Column(Integer, primary_key=True, index=True)
#     name = Column(String, nullable=False)
#     department = Column(String, nullable=False)
#     performance_score = Column(Integer)  # e.g., 1 to 10
#     attendance_pct = Column(Float)       # e.g., 95.5
#     attrition_score = Column(Float)      # e.g., 0.1 to 1.0 (likelihood to leave)
#     salary = Column(Integer)

from sqlalchemy import Column, Integer, String, Float, Date
from database.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)


class Employee(Base):
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True, index=True)

    employee_code = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False, index=True)
    gender = Column(String, nullable=True)
    date_of_birth = Column(Date, nullable=True)

    email = Column(String, nullable=True, index=True)
    mobile_number = Column(String, nullable=True)

    department = Column(String, nullable=True, index=True)
    designation = Column(String, nullable=True)

    joining_date = Column(Date, nullable=True)
    employment_status = Column(String, nullable=True, index=True)

    performance_score = Column(Float, nullable=True)
    attendance_pct = Column(Float, nullable=True)
    attrition_score = Column(Float, nullable=True)
    salary = Column(Float, nullable=True)

    leave_balance = Column(Float, nullable=True)
